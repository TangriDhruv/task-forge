"""Playwright-backed browser controller: observation + action execution.

Design choice (see CLAUDE.md / REPORT.md §1 for the full reasoning): observation
is DOM-derived structured data — the interactive elements on the page, plus the
visible body text — not a screenshot. That is what still works on a legacy,
non-semantic page: the same information a human operator reads off the screen,
without needing test IDs or a clean accessibility tree.

Locator robustness is decided once, here, per element (`_compute_locator_chains`),
as a *ranked chain* in priority order: `id` > `name` > unique href (links) >
unique visible text > CSS position among same-type elements as a last
resort — every applicable tier, not just the best one. That full chain is
the "reasoning about robustness" the artifact schema (assignment.md §3.2)
records directly (`cua.artifact.schema.LocatorSpec`/`Step.locator_chain`);
nothing reconstructs it later. Discovery and replay also *resolve* a chain
identically, via the shared `resolve_locator_chain` — see `act_click` etc.
below — so a locator that's "robust enough to replay" means the same thing
in both places.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Dialog, Page, Playwright, sync_playwright

from cua.artifact.schema import LocatorResolutionError, LocatorSpec, resolve_locator_chain
from cua.safety.allowlist import AllowlistPolicy

# Collects every plausibly-interactive, visible element on the page, in DOM
# order, with just enough attributes to both describe it to the LLM and to
# compute a locator for it. Visibility is checked in-browser (bounding box +
# computed style) so we don't burn a Python round-trip per element.
_JS_COLLECT_ELEMENTS = """
() => {
  function visible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
      && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const nodes = Array.from(document.querySelectorAll('a[href], button, input, select, textarea'));
  return nodes.filter(visible).map((el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let text = '';
    if (tag === 'select') {
      text = '';
    } else if ('value' in el && (type === 'submit' || type === 'button')) {
      text = (el.value || '').trim();
    } else {
      text = (el.innerText || el.getAttribute('placeholder') || '').trim();
    }
    return {
      tag, type,
      name: el.getAttribute('name') || '',
      id: el.getAttribute('id') || '',
      href: tag === 'a' ? (el.getAttribute('href') || '') : '',
      text: text.slice(0, 200),
      options: tag === 'select' ? Array.from(el.options).map(o => o.value) : null,
      checked: type === 'checkbox' ? el.checked : null,
    };
  });
}
"""


def _role_for(el: dict) -> str:
    tag, type_ = el["tag"], el["type"]
    if tag == "a":
        return "link"
    if tag == "button" or type_ in ("submit", "button"):
        return "button"
    if type_ == "checkbox":
        return "checkbox"
    if tag == "select":
        return "combobox"
    return "textbox"


def _compute_locator_chains(raw_elements: list[dict]) -> list[tuple[LocatorSpec, ...]]:
    """One ranked fallback chain per element, not just its single best
    locator: every tier that's actually valid for this element, in priority
    order (`id` > `name` > unique href > unique text), with `css_nth` always
    appended last as the final resort. Discovery and replay both resolve a
    chain by trying it top to bottom (`resolve_locator_chain`), so recording
    every usable tier here -- not only the first -- is what gives replay
    something to fall back on if a higher tier ever stops resolving."""
    text_counts: dict[tuple[str, str], int] = {}
    for el in raw_elements:
        key = (el["tag"], el["text"])
        text_counts[key] = text_counts.get(key, 0) + 1

    href_counts: dict[str, int] = {}
    for el in raw_elements:
        if el["tag"] == "a" and el["href"]:
            href_counts[el["href"]] = href_counts.get(el["href"], 0) + 1

    selector_seen: dict[str, int] = {}
    chains: list[tuple[LocatorSpec, ...]] = []
    for el in raw_elements:
        tag = el["tag"]
        selector = f"{tag}[type='{el['type']}']" if el["type"] else tag
        nth = selector_seen.get(selector, 0)
        selector_seen[selector] = nth + 1

        chain: list[LocatorSpec] = []
        if el["id"]:
            chain.append(LocatorSpec(by="id", value=el["id"]))
        if el["name"]:
            chain.append(LocatorSpec(by="name", value=el["name"]))
        if tag == "a" and el["href"] and href_counts[el["href"]] == 1:
            # More stable than visible text for links: rows in a table often
            # repeat the same link text ("View Trust Center Page") but always
            # point at a distinct, meaningful href.
            chain.append(LocatorSpec(by="href", value=el["href"]))
        if el["text"] and text_counts[(tag, el["text"])] == 1:
            chain.append(LocatorSpec(by="role_text", value=el["text"], tag=tag))
        # Always present, always last: the one tier guaranteed to exist for
        # every element, however brittle (position shifts if the page's
        # layout changes -- that's exactly the drift it's meant to survive
        # only as a last resort, not the common case).
        chain.append(LocatorSpec(by="css_nth", value=selector, tag=selector, nth=nth))
        chains.append(tuple(chain))
    return chains


@dataclass
class ObservedElement:
    ref: int
    tag: str
    role: str
    text: str
    attrs: dict
    locator_chain: tuple[LocatorSpec, ...]

    def to_prompt_dict(self) -> dict:
        d = {"ref": self.ref, "role": self.role, "text": self.text}
        if self.attrs.get("name"):
            d["name"] = self.attrs["name"]
        if self.attrs.get("href"):
            d["href"] = self.attrs["href"]
        if self.attrs.get("options"):
            d["options"] = self.attrs["options"]
        if self.attrs.get("checked") is not None:
            d["checked"] = self.attrs["checked"]
        return d


@dataclass
class Observation:
    url: str
    title: str
    page_text: str
    elements: list[ObservedElement]
    dialogs_seen: list[str]

    def to_prompt_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "visible_text": self.page_text,
            "interactive_elements": [e.to_prompt_dict() for e in self.elements],
            "dialogs_since_last_step": self.dialogs_seen,
        }


class ActionError(RuntimeError):
    """An action failed at the browser level (element missing, timeout, etc.)."""


class BrowserSession:
    def __init__(self, policy: AllowlistPolicy, headless: bool = True):
        self.policy = policy
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser = None
        self.page: Page | None = None
        self._pending_dialogs: list[str] = []

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self.page = self._browser.new_page()
        self.page.on("dialog", self._handle_dialog)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def _handle_dialog(self, dialog: Dialog) -> None:
        # A human operator would read the dialog and click through it; we do
        # the same (accept) and record it so it shows up as a handled
        # interstitial in the run's evidence, per the error taxonomy in
        # assignment.md §3.3 ("recoverable conditions ... dismiss a known
        # interstitial").
        self._pending_dialogs.append(f"{dialog.type}: {dialog.message}")
        dialog.accept()

    def _drain_dialogs(self) -> list[str]:
        seen, self._pending_dialogs = self._pending_dialogs, []
        return seen

    def goto(self, url: str) -> None:
        self.policy.check_url(url)
        self.page.goto(url, wait_until="load")

    def login(self, base_url: str, username: str | None, password: str | None) -> None:
        # Deliberately not LLM-driven: authentication is session setup, not
        # part of the goal-specific capability being discovered. See
        # REPORT.md §1.
        #
        # Optional: a known internal app assumes an already-authenticated
        # session and needs this fixed-shape bootstrap; a real public site
        # (e.g. a vendor's trust-center page) typically needs no login at
        # all, and almost certainly doesn't match this exact form shape
        # even if it has one. username=None (or password=None) skips this
        # entirely rather than forcing every target to look like our own
        # mock apps' login page.
        if username is None or password is None:
            return
        self.goto(f"{base_url}/login")
        self.page.fill("input[name='username']", username)
        self.page.fill("input[name='password']", password)
        self.page.click("input[type=submit], button[type=submit]")
        self.page.wait_for_load_state("load")

    def observe(self, max_text_chars: int = 4000) -> Observation:
        raw = self.page.evaluate(_JS_COLLECT_ELEMENTS)
        chains = _compute_locator_chains(raw)
        elements = [
            ObservedElement(ref=i, tag=el["tag"], role=_role_for(el), text=el["text"], attrs=el, locator_chain=chain)
            for i, (el, chain) in enumerate(zip(raw, chains))
        ]
        body_text = self.page.evaluate("document.body.innerText") or ""
        return Observation(
            url=self.page.url,
            title=self.page.title(),
            page_text=body_text.strip()[:max_text_chars],
            elements=elements,
            dialogs_seen=self._drain_dialogs(),
        )

    def _assert_still_in_policy(self) -> None:
        self.policy.check_url(self.page.url)

    def _resolve(self, element: ObservedElement) -> tuple:
        # Same resolution function replay uses -- see module docstring.
        # Returns (playwright_locator, tier_index); tier_index > 0 means a
        # fallback had to be used, which is the UI-drift signal.
        try:
            return resolve_locator_chain(self.page, element.locator_chain)
        except LocatorResolutionError as exc:
            raise ActionError(str(exc)) from exc

    def act_click(self, element: ObservedElement) -> None:
        self.policy.check_action_type("click")
        try:
            locator, _tier = self._resolve(element)
            locator.click(timeout=5000)
            self.page.wait_for_load_state("load", timeout=10000)
        except ActionError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize to ActionError for the loop
            raise ActionError(f"click on {element.locator_chain[0].describe()} failed: {exc}") from exc
        self._assert_still_in_policy()

    def act_type(self, element: ObservedElement, text: str, submit: bool = False) -> None:
        self.policy.check_action_type("type_text")
        try:
            locator, _tier = self._resolve(element)
            locator.fill(text, timeout=5000)
            if submit:
                locator.press("Enter")
                self.page.wait_for_load_state("load", timeout=10000)
        except ActionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ActionError(f"type into {element.locator_chain[0].describe()} failed: {exc}") from exc
        self._assert_still_in_policy()

    def act_select(self, element: ObservedElement, value: str) -> None:
        self.policy.check_action_type("select_option")
        try:
            locator, _tier = self._resolve(element)
            locator.select_option(value, timeout=5000)
        except ActionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ActionError(f"select {value!r} on {element.locator_chain[0].describe()} failed: {exc}") from exc
        self._assert_still_in_policy()

    def screenshot(self, path: str) -> None:
        self.page.screenshot(path=path, full_page=True)
