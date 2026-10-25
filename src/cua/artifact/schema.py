"""The artifact schema: a reusable, agent-invocable capability (assignment.md §3.2).

An ``Artifact`` is a typed contract, not a step recording: inputs/outputs a
calling agent binds to, a fixed step list with a frozen locator chain per
step (see ``REPORT.md`` §3 for why targeting must be decided once, at
discovery time, never re-derived at replay time), a checkpoint that proves
the goal was actually reached, and an ``outcome_table`` that turns runtime
error handling into data the artifact carries rather than logic the replay
engine special-cases per app.

Every dataclass here is frozen/plain data on purpose: nothing in this module
makes a decision, it only describes one that was already made during
discovery (or, for ``event_library`` rows, proposed by discovery and later
approved by a human — see ``store.py``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

ActionType = Literal["click", "type_text", "select_option", "wait_for"]
ConditionKind = Literal["url_matches", "text_contains", "element_present", "element_absent"]
OutcomeClass = Literal["business", "recoverable", "hard_failure"]
LocatorBy = Literal["id", "name", "href", "role_text", "css_nth"]
ParamType = Literal["string", "number", "boolean"]
ExtractionKind = Literal["label_value", "regex"]


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat('" + "', \"'\", '".join(parts) + "')"


@dataclass(frozen=True)
class LocatorSpec:
    """One way to find an element. Several of these, ranked, form a Step's
    ``locator_chain`` -- both discovery (``cua.agent.browser``) and replay
    resolve the *same* chain the *same* way via ``resolve_locator_chain``
    below, so "how robust is this locator" is answered identically
    everywhere it matters, not reimplemented per subsystem.

    Playwright is only imported lazily, inside ``to_playwright``, so this
    module stays importable (e.g. for artifact storage/review tooling)
    without Playwright installed -- see REPORT.md §4 on the surface seam.
    """

    by: LocatorBy
    value: str
    tag: str | None = None
    nth: int = 0

    def describe(self) -> str:
        if self.by == "css_nth":
            return f"{self.by}:{self.value}[{self.nth}]"
        return f"{self.by}:{self.value}"

    def to_playwright(self, page: "Page") -> "Locator":
        if self.by == "id":
            return page.locator(f"#{self.value}")
        if self.by == "name":
            return page.locator(f"[name='{self.value}']")
        if self.by == "href":
            return page.locator(f"a[href='{self.value}']")
        if self.by == "role_text":
            if self.tag == "input":
                return page.locator(f"xpath=//input[@value={_xpath_literal(self.value)}]")
            return page.locator(f"xpath=//{self.tag}[normalize-space(.)={_xpath_literal(self.value)}]")
        if self.by == "css_nth":
            return page.locator(self.value).nth(self.nth)
        raise ValueError(f"unknown locator strategy: {self.by}")


class LocatorResolutionError(RuntimeError):
    """Every tier in a locator chain either didn't resolve, resolved to more
    than one element, or timed out."""


def resolve_locator_chain(
    page: "Page", chain: "tuple[LocatorSpec, ...]", timeout_ms: int = 3000
) -> "tuple[Locator, int]":
    """Try each ``LocatorSpec`` in order; return the first that resolves to
    *exactly one* element, plus its tier index (0 = primary, higher = a
    fallback had to be used -- the UI-drift signal). Raises
    ``LocatorResolutionError`` with every tier's failure reason if the whole
    chain is exhausted."""
    attempts: list[str] = []
    for tier, spec in enumerate(chain):
        try:
            locator = spec.to_playwright(page)
            count = locator.count()
            if count == 1:
                return locator, tier
            attempts.append(f"{spec.describe()}: resolved to {count} elements, expected 1")
        except Exception as exc:  # noqa: BLE001 -- any tier can fail; keep trying the rest
            attempts.append(f"{spec.describe()}: {exc}")
    raise LocatorResolutionError(
        f"no locator in the chain resolved to exactly one element: {'; '.join(attempts)}"
    )


@dataclass(frozen=True)
class Condition:
    """A closed-vocabulary assertion about the current page -- never free-form
    code. ``value`` semantics depend on ``kind``:
      - url_matches:      substring the current URL must contain
      - text_contains:    substring the page's visible text must contain
      - element_present:  a descriptor (role/text) that must appear among the
                           current interactive elements
      - element_absent:   same, but must NOT appear
    """

    kind: ConditionKind
    value: str

    def describe(self) -> str:
        return f"{self.kind}={self.value!r}"


def condition_holds(condition: Condition, page_text: str, url: str, element_descriptors: list[str]) -> bool:
    """Does this Condition currently hold? One evaluator for the whole
    closed vocabulary, shared by replay's post_condition/checkpoint checks,
    an artifact's local ``outcome_table``, and the shared ``event_library``'s
    ``match_event`` (store.py) -- never reimplemented per caller.

    ``element_descriptors`` is a flat list of short strings describing each
    currently-observed interactive element (e.g. ``"button:Confirm"``), so
    element_present/element_absent can match without needing free-form code.
    """
    if condition.kind == "url_matches":
        return condition.value in url
    if condition.kind == "text_contains":
        return condition.value in page_text
    if condition.kind == "element_present":
        return any(condition.value in d for d in element_descriptors)
    if condition.kind == "element_absent":
        return not any(condition.value in d for d in element_descriptors)
    return False


@dataclass(frozen=True)
class InputParam:
    name: str
    type: ParamType
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class Extraction:
    """How to pull one output's value off the page at replay time -- a
    closed, deterministic alternative to free-form scraping code.

    - label_value: find a line of visible text containing ``pattern`` as a
      label, take whatever follows it on that line (tab/run-of-whitespace
      separated) as the value. Matches exactly how this project's legacy,
      table-rendered confirmation screens present data -- "Transfer ID<TAB>
      XFR-233353" -- without depending on any markup structure beyond
      label-then-value, which is what a human reads off the screen too.
    - regex: ``re.search(pattern, page_text)``, pattern must have exactly
      one capture group. The fallback for values that aren't label/value
      pairs (e.g. embedded in a sentence).
    """

    kind: ExtractionKind
    pattern: str


@dataclass(frozen=True)
class OutputField:
    name: str
    type: ParamType
    description: str = ""
    extract: Extraction | None = None


@dataclass(frozen=True)
class Step:
    step_id: str
    action_type: ActionType
    locator_chain: tuple[LocatorSpec, ...]
    # Optional on purpose: not every intermediate step has a cheap, general
    # (non-parameter-specific) way to verify it landed correctly -- e.g. a
    # dropdown selection doesn't navigate anywhere. Where it's None, replay
    # relies on the action itself raising (e.g. Playwright's own timeout) and
    # on the artifact's single overall `checkpoint`, checked at the end, to
    # catch anything a per-step check would have (see REPORT.md §3/§7: a
    # documented cut, not a silent gap -- the schema supports a stricter
    # per-step check as a reviewer could add one).
    post_condition: Condition | None = None
    # Which artifact-level input feeds this step's typed value (type_text /
    # select_option), if any. A step with no binding and no literal is a pure
    # navigation/click step.
    input_binding: str | None = None
    text_literal: str | None = None
    select_value: str | None = None


@dataclass(frozen=True)
class OutcomeRule:
    """One row of an artifact's local outcome table. Shape matches
    ``event_library`` rows in ``store.py`` exactly on purpose -- a rule here
    is just one that hasn't (yet) been promoted to the shared, cross-artifact
    table."""

    signature: Condition
    outcome_class: OutcomeClass
    outcome_name: str


@dataclass
class Artifact:
    artifact_id: str
    name: str
    version: int
    goal_description: str
    app_id: str
    base_url_pattern: str
    inputs: list[InputParam]
    outputs: list[OutputField]
    steps: list[Step]
    checkpoint: Condition
    outcome_table: list[OutcomeRule] = field(default_factory=list)
    # Sparse per-tenant patch: only the steps whose locator actually changed
    # need an entry. See REPORT.md §4 (cross-tenant reuse).
    tenant_overrides: dict[str, dict[str, tuple[LocatorSpec, ...]]] = field(default_factory=dict)
    created_from_run: str | None = None
    reviewed: bool = False

    # -- serialization -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        def _condition(c: dict) -> Condition:
            return Condition(kind=c["kind"], value=c["value"])

        def _locator_chain(chain: list[dict]) -> tuple[LocatorSpec, ...]:
            return tuple(
                LocatorSpec(by=l["by"], value=l["value"], tag=l.get("tag"), nth=l.get("nth", 0))
                for l in chain
            )

        def _step(s: dict) -> Step:
            post_condition = s.get("post_condition")
            return Step(
                step_id=s["step_id"],
                action_type=s["action_type"],
                locator_chain=_locator_chain(s["locator_chain"]),
                post_condition=_condition(post_condition) if post_condition else None,
                input_binding=s.get("input_binding"),
                text_literal=s.get("text_literal"),
                select_value=s.get("select_value"),
            )

        def _outcome_rule(o: dict) -> OutcomeRule:
            return OutcomeRule(
                signature=_condition(o["signature"]),
                outcome_class=o["outcome_class"],
                outcome_name=o["outcome_name"],
            )

        def _output_field(o: dict) -> OutputField:
            extract = o.get("extract")
            return OutputField(
                name=o["name"],
                type=o["type"],
                description=o.get("description", ""),
                extract=Extraction(kind=extract["kind"], pattern=extract["pattern"]) if extract else None,
            )

        return cls(
            artifact_id=d["artifact_id"],
            name=d["name"],
            version=d["version"],
            goal_description=d["goal_description"],
            app_id=d["app_id"],
            base_url_pattern=d["base_url_pattern"],
            inputs=[InputParam(**p) for p in d["inputs"]],
            outputs=[_output_field(o) for o in d["outputs"]],
            steps=[_step(s) for s in d["steps"]],
            checkpoint=_condition(d["checkpoint"]),
            outcome_table=[_outcome_rule(o) for o in d.get("outcome_table", [])],
            tenant_overrides={
                tenant: {
                    step_id: _locator_chain(chain) for step_id, chain in overrides.items()
                }
                for tenant, overrides in d.get("tenant_overrides", {}).items()
            },
            created_from_run=d.get("created_from_run"),
            reviewed=d.get("reviewed", False),
        )

    @classmethod
    def from_json(cls, s: str) -> "Artifact":
        return cls.from_dict(json.loads(s))
