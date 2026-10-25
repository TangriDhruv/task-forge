# Design write-up

See [CLAUDE.md](CLAUDE.md) for the full, dated log of every decision and why it was made, and
[docs/assignment.md](docs/assignment.md) for the brief these headings respond to (§6.2). Every
claim below that says "verified live" or similar was run against the real target app this
session, not simulated — see `evidence/` and the demo path in [README.md](README.md).

## 1. Architecture

**Target app:** a self-built mock credit-union servicing console — an on-domain banking surface
matching the brief's own example goals (§2) almost verbatim, in preference to an adjacent domain
the author had more personal experience with. Read flow: search member → member detail → read a
savings balance. Write flow: open a new sub-account via a justification form → confirmation. A
third, "risky" action — transfer funds between accounts — is gated behind an explicit
confirmation checkbox, giving the safety model (§6) a concrete action to classify rather than
only arguing the read/write distinction in prose. Legacy-styled on purpose: nested `<table>`
layout, no `id`/`class`/`data-test` attributes anywhere, `name` attributes only (forms need them
to submit) — verified by grep, zero such attributes exist in any template. Five fixed synthetic
members are seeded, each engineered to hit one scenario end to end: a happy path, a frozen
account, insufficient funds on a transfer, a restricted member (permission-denied on writes), and
a closed member (a not-found-style outcome).

**Two separate processes, never one.** The target app has no idea it's being automated; both
discovery and replay reach it the same way a human would, through one Playwright-controlled
browser. This isn't incidental — it's the premise the whole brief rests on (no API, can't modify
the vendor's code), and collapsing the two into one process would quietly assume that premise
away. It also pays for itself twice: the automation side can point at *any* tenant's instance of
the same vendor app without touching that app's code, and the escalation bridge (§5) needs a live
browser session it can expose to a human from *outside* — which only makes sense if that session
already lives in a process the target app never sees.

**Single process, no queue, for the automation side itself.** One Flask app (`src/cua/app/`) is
the primary, interactive interface — not a CLI. It owns the discovery loop, the Playwright
browser, and, only when a run pauses for help, a small in-process escalation surface, all on one
background thread per run. A scriptable, non-interactive CLI (`cua.agent.cli`, `cua.replay.cli`)
exists alongside it for evidence capture (`run-demo`) and for the production invocation shape an
AI agent product would actually use (`replay()` as a direct function call) — the dashboard is for
a human watching a run, not the production path.

**Discovery agent:** Playwright (Python) driven by a Claude tool-use loop (`click` / `type_text`
/ `select_option` / `finish_success` / `request_human_help` / `finish_stuck`). Observation is
DOM-derived structured JSON — visible interactive elements plus page text, not screenshots —
because that's what still works with no clean DOM: no dependency on `id`/`class`/test attributes,
only on what a human operator could also read off the rendered page. Login happens as a
deterministic bootstrap step before the LLM loop starts: authentication is session setup, not
part of the task-specific capability being discovered.

## 2. Artifact schema

An `Artifact` (`src/cua/artifact/schema.py`) is a typed contract, not a step recording: inputs/
outputs a calling agent binds to, a fixed `Step` list each carrying a *ranked* `locator_chain`
(frozen at discovery time, never re-derived at replay time), a `checkpoint` condition that proves
the goal was actually reached, and an `outcome_table` that makes error handling data the artifact
carries rather than logic the replay engine special-cases per app. `Condition` — the only thing
anything ever gets matched against (`url_matches` / `text_contains` / `element_present` /
`element_absent`) — is a closed vocabulary on purpose: never free-form code, so nothing matched
against it can be a surprise. One evaluator, `condition_holds`, checks every Condition everywhere
one is used — a step's optional `post_condition`, the final `checkpoint`, the local
`outcome_table`, and the shared `event_library` below — so "does this hold" means the same thing
in all four places, not four slightly-different reimplementations.

**Locator chains are real ranked fallbacks, not a single best guess.**
`_compute_locator_chains` (`cua.agent.browser`) builds every applicable tier per element — `id` →
`name` → unique `href` → unique text → `css_nth` (always last) — and `resolve_locator_chain`
(`cua.artifact.schema`) is the *one* function that resolves a chain against a live page.
Discovery's `act_click`/`act_type`/`act_select` and replay's executor both call it, so "robust
enough to replay" means the same thing in both places, and a fallback tier succeeding is recorded
as drift rather than silently ignored. Verified live: a real discovery run produced chains like
`['href:/member/M-10001', 'role_text:M-10001', 'css_nth:a[3]']` for every element, and every
subsequent replay resolved at tier 0 (no drift) until deliberately tested otherwise.

**Output extraction is a closed, deterministic vocabulary, never scraping code.** An
`OutputField.extract` is either `label_value` (find a line of visible text containing a label,
take whatever follows it on that line — exactly how this app's confirmation screens render
"Transfer ID␉XFR-233353") or `regex` (one capture group, for values embedded in a sentence). No
output is ever extracted by guessing at markup structure.

**Shared event memory, not per-artifact-only.** Every artifact's `outcome_table` entry is backed
by the same shape of row in a SQLite-backed `event_library` (`cua.artifact.store`), keyed by
structural `Condition`, not by app — a pattern learned on one artifact is available to any other
artifact that hits the same signature, which is the concrete answer to "reusable for any browser
automation without an API." Discovery may `propose_event` a new row (`status: pending_review`)
when it meets a page state no rule explains; only a human's `review_event` call can move it to
`approved`, and `match_event` — the only function replay may call — only ever reads `approved`
rows. An LLM may *propose* a pattern, never *approve* its own pattern, because anything
judgment-based reaching replay would break §3's non-negotiable constraint. SQLite, not a vector
store, for the same reason: event matching is exact structural matching, not similarity search,
because similarity search is itself a judgment call.

**The recorder (`cua.artifact.recorder.record_artifact`) is mechanical and explicit, never
inferred.** The caller supplies `bindings` — `param_name -> the concrete value used in this
specific discovery run` (e.g. `member_id="M-10001"`) — and the recorder finds every occurrence of
those values inside locator values and typed/selected values and replaces them with
`{param_name}` placeholders; a typed/selected value that *exactly* equals a bound value becomes
an `input_binding` instead. Nothing is ever guessed at being a parameter. Verified live: the
`cu-transfer-funds` artifact, recorded from a run that used `amount=50`, replayed successfully
with `amount=25` — proof the parameterization is real, not a baked-in recording with cosmetic
flexibility.

## 3. Determinism & error handling

Determinism here is two separate guarantees, both satisfied by construction, not by convention:

- **Control-flow determinism:** replay walks `artifact.steps`, a fixed list decided once during
  discovery. Nothing in `src/cua/replay/` imports the LLM or the discovery loop — enforced by
  `tests/test_replay.py`'s static import-graph check, not just a comment.
- **Targeting determinism:** every step's locator chain was frozen at discovery time (§2); replay
  never re-derives where to click.

**Error handling is a lookup, not a judgment call, checked in a fixed order.** At every step, and
at the end: a known outcome (artifact-local `outcome_table`, then the shared `event_library`)
*always* wins over "couldn't find the element" or "the action errored" — a page that would
otherwise look like a failure might just as well be a legitimate, already-known business
outcome. Verified live: replaying `cu-transfer-funds` with a frozen account's ID correctly
returns `business_outcome("account_frozen")`; with an amount exceeding a real balance,
`business_outcome("insufficient_funds")` — neither a crash nor a silent wrong answer. Only after
every known-outcome check fails does an unresolved locator or action error become a hard
`failure`, reported with the failing step, what was expected, and what was actually observed.

**Recoverable outcomes get one bounded, specific recovery attempt, not open-ended retry logic.**
Dialog auto-accept already happens for free at the `BrowserSession` level (shared by discovery
and replay), so the one case that needed new code is a session-timeout page, recovered by logging
back in once (`_attempt_recovery`). Anything else classified `recoverable` falls through to the
next check unchanged — an honest "detected but not auto-recovered" result, not invented
sophistication (see §7).

**Fixture-state determinism (target app):** a successful transfer records a ticket but
deliberately does not debit/credit the seeded account balances — only opening a sub-account
mutates seed state, since that mutation is the one thing a replay needs to observe to confirm a
write happened. Fixed balances are what keep the insufficient-funds and frozen-account outcomes
repeatable across many runs in the same process, rather than a scenario silently stopping to
reproduce after the first run drains an account.

**UI drift** is the secondary concern the brief asks to address: a locator chain falling back to
a lower tier (`tier_used > 0` in a replay's result) is the signal, recorded per step without
treating it as a failure — the artifact still worked, just via a less-preferred path, worth a
human's attention on review rather than an automatic alarm.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam between "how we perceive/act on a surface" and "the recorded
flow" is already explicit: `Step` only ever references an `action_type` from a closed, four-verb
vocabulary and a `locator_chain` of typed specs — nothing in the artifact schema mentions
Playwright, HTML, or this specific app. `LocatorSpec.to_playwright` is the *only* place that
vocabulary turns into a concrete DOM operation, imported lazily so the schema module itself has
no import-time Playwright dependency. Extending to a legacy frameset app needs no schema change,
only a locator-computation strategy that can address frames. Extending to a desktop app would
swap `LocatorSpec`'s `by` values for an accessibility-tree role/name addressing scheme and the
executor's action calls for an OS-automation backend — the `Step`/`Condition`/`OutcomeRule`
vocabulary above it is already surface-agnostic, since every condition it evaluates (a URL, a
text substring, an element descriptor) has a natural analogue outside the browser too.

**Multi-tenant reuse.** `Artifact.tenant_overrides: dict[tenant_id, dict[step_id, locator_chain]]`
is already in the schema as a sparse patch — only the steps whose locator actually changed for a
given tenant need an entry; everything else inherits unchanged. The `event_library`'s structural
signatures are the same mechanism applied to *error handling* reuse: a pattern learned on one
tenant's instance of an app is available to any other artifact/tenant that hits the same
structural signature, without re-discovery. **Not built this session:** a second, relabeled
"Tenant B" configuration of the target app to exercise `tenant_overrides` live — this is the
single largest intentional cut (§7), left as a design that's load-bearing in the schema rather
than demonstrated end to end, given the time-box.

**Drift detection across tenants/versions** reuses the same per-step `tier_used` signal from §3:
a tenant whose markup has drifted from the base recording shows up as elevated fallback-tier
usage on replay, which is the natural trigger for "this tenant needs a `tenant_overrides` entry"
— a detection mechanism that already exists, not a separate one to build.

## 5. Escalation & handoff

**Detecting "stuck" is two distinct signals, not one.** `finish_stuck` is terminal — the goal
looks impossible, or a reasonable approach already failed; the run ends and needs a person to
investigate from scratch. `request_human_help` is bounded — the agent needs one specific thing
done (a credential it doesn't have, a CAPTCHA, a risky step needing a yes/no) after which it
expects to keep going on the *same* task. Conflating these would make every escalation look like
a failure; keeping them separate is what makes "pause, hand off, resume" meaningfully different
from "give up."

**Taking control of the live session — not a mock, not a literal `<iframe>`.** A naive
`<iframe src="the-target-app-url">` would load in the *viewer's own* browser, with its own fresh,
unauthenticated cookie jar, completely disconnected from the Playwright session the agent is
actually driving — it would show a logged-out page, not what the agent sees, and clicking inside
it would touch nothing real. Instead, `EscalationBridge` (`cua.escalation.bridge`) blocks the
discovery thread on a `threading.Event` the moment `request_human_help` is called, flips
`control` to `"human"`, and keeps the Playwright page open. The dashboard polls a
*viewport-sized* screenshot (not full-page — click coordinates are viewport-relative) and relays
clicks/keystrokes straight onto that *same* live `Page` via `page.mouse.click`/
`page.keyboard.type`. This is real control transfer onto the same session — the thin version of
real-time co-browsing the brief explicitly permits mocking, built as the actual mechanism rather
than a stand-in for it.

**Resuming and recording what the human did.** Calling `resume()` unblocks the discovery thread,
which continues from whatever page state the human left behind. Every relayed action is appended
to the run's trace with `actor: "human"` — satisfying "record what the human did" directly,
not as an afterthought. A sensitive action (a password being typed) is recorded as only its
length, never its value, enforced at the point of capture (`relay_type`'s `sensitive` flag), so a
human's intervention can never leak a secret into evidence even by accident.

**The credential store is the one place a secret is allowed to persist, and only by explicit
opt-in.** If a human types a value and also names it to save, it's encrypted (Fernet, a key kept
in `.env` alongside `ANTHROPIC_API_KEY`) into its own table in `data/cua.db`; a later run that
names the same credential resolves it automatically, with no pause at all. This is compatible
with §6's hard constraint — "never persist secrets... into artifacts or logs" — because this
store is neither of those: never embedded in an artifact, never echoed into a trace or log.

**Verified mechanically, not just designed:** an HTTP-level integration test drives the actual
Flask routes — pause, screenshot fetch, click relay, type a password with `save_as` set, confirm
it's encrypted and retrievable, confirm the recorded action shows only `{"length": 12}`, resume,
confirm control returns to `"agent"`.

## 6. Safety

One `AllowlistPolicy` object, constructed once per run and shared by every path that touches the
browser — discovery, replay, and escalation's relay — so "what's allowed" is defined once, never
two copies that can drift apart.

| Layer | What it restricts | Enforced where |
|---|---|---|
| Domain/origin allowlist | Can never navigate off the permitted origin | Every `goto`, and after every action (`check_url`) |
| Action-vocabulary allowlist | Only a closed verb set exists at all — no eval, no shell, no file access | The tool surface itself; replay's executor only knows the same four verbs |
| Risk classification | Every action tagged safe (read) or risky (write/irreversible) | `AllowlistPolicy.is_risky` |
| Confirmation gate | The one risky action (fund transfer) requires an explicit checkbox; unchecked, the target app itself re-renders the form with a correction message rather than executing | Target app; mirrored at replay by the checkbox being a normal, required form field in the recorded flow |
| Redaction | Sensitive values never reach a trace/log in raw form | `EscalationBridge.relay_type`'s `sensitive` flag at the point of capture; `cua.safety.redact` |
| Credential encryption | A saved secret is never stored in plaintext, never embedded in an artifact | `cua.escalation.credentials` (Fernet, separate table) |

**Limits, stated rather than left implicit:** the allowlist is origin + action-type, not a
fine-grained per-route policy (e.g. "read-only on `/member/*`, no writes") — a credible next step,
not built, because the brief's one risky action already had an adequate gate without it. Redaction
is enforced at the one path that actually carries secrets today (the escalation relay); a future
input type carrying sensitive data through the ordinary discovery/replay path (not via escalation)
would need the same discipline applied there too — worth flagging rather than assuming it's
already covered everywhere by extension.

## 7. Cuts

What's deliberately thin, stated rather than left as a silent gap:

- **A second "Tenant B" target-app variant was not built.** `tenant_overrides` is real in the
  schema and the design reasoning is in §4, but it's never been exercised against an actual
  second, relabeled app instance. Next step if continued: a second Flask config of the same
  target app with renamed fields/reordered columns, and one artifact replayed against both.
- **Per-step `post_condition` is left `None` by the recorder.** The schema supports a stricter
  per-step check; deriving a *general* (non-member-specific) one automatically from a single
  run's before/after diff is exactly the kind of fuzzy inference the recorder's design avoids
  (§2) — a human reviewing a recorded artifact can add one by hand. The artifact's single overall
  `checkpoint`, checked at the end, is what's guaranteed today.
- **`recoverable` outcome handling covers one real case** (session timeout → re-login once) plus
  whatever `BrowserSession`'s dialog auto-accept already covers for free. A transient-slowness
  retry-with-backoff is designed-for (the outcome class exists, the check-and-continue shape is
  there) but not implemented as its own behavior.
- **The allowlist is origin + action-type**, not fine-grained per-route (§6).
- **Stretch goals (§8 of the brief) were not attempted**, by design, in favor of depth on the
  core thread: no capability-catalog API, no code-generation from an artifact, no approval-gate
  state machine beyond the `reviewed: bool` field already present, no multi-run stability
  harness, no cross-tenant demo (see above). Each would be a natural, bounded addition on top of
  what exists — the schema and executor were built with slots for exactly these (`reviewed`,
  `tenant_overrides`, `event_library`'s `pending_review` gate) rather than needing rework first.
