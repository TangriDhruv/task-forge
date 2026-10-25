# CLAUDE.md

This file orients any Claude Code session (or subagent) working in this repo. Read this before
writing code. The full assignment brief is saved verbatim at
[docs/assignment.md](docs/assignment.md) — treat it as the source of truth; this file is a
distilled, project-specific operating guide, not a replacement for it.

## North star

This repo is a take-home submission for interface.ai: a computer-use automation system for
banks/credit unions' legacy back-office apps that have no API. The through-line, quoted
directly from the brief, governs every design decision here:

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how
> the AI agent invokes it in production.

Concretely, one thread must run end to end: **goal → LLM-driven discovery run → saved
artifact → deterministic replay (with error/outcome handling) → human-escalation path that can
take over the live session → evidence for both runs.** Every core requirement in
`docs/assignment.md` §3 must be touched, even if thin — do not polish one piece while leaving
another as a TODO.

## Locked decisions

- **Language/runtime:** Python.
- **LLM:** Anthropic Claude, called with the user's own API key. The key lives in `.env`
  (gitignored) as `ANTHROPIC_API_KEY`; `.env.example` documents the shape. **Never commit
  `.env`, never print the key in logs.** If the key isn't itself scoped to a workspace, the API
  rejects every call with a 400 until `ANTHROPIC_WORKSPACE_ID` (also in `.env`) is set —
  `cua.agent.llm.Decider` sends it as the `anthropic-workspace-id` header automatically when
  present. Not a secret the way the key is, but kept alongside it out of version control anyway.
  Extended thinking is on for this account by default; `Decider._call_model` truncates each
  turn's recorded content to the first `tool_use` block (dropping any extra ones the model
  sometimes emits in the same turn despite `tool_choice: "any"`) — without this, a second,
  unexecuted `tool_use` block was discovered live to leave the message history permanently
  broken (the API rejects the *next* call with "tool_use ids were found without tool_result
  blocks"). Regression-tested in `tests/test_llm.py`.
- **Target/proxy application: a self-built mock credit-union servicing console** — an
  on-domain banking surface, matching the brief's own example goals in §2 almost verbatim.
  **Supersedes the earlier GRC/vendor-review-portal choice**, reversed on review: an on-domain
  banking surface is a stronger, more direct answer to this brief than an adjacent domain, even
  though the author has more personal experience building GRC/trust-center tooling.
  - **Read flow:** `/search` (member search) → `/member/<id>` (profile + accounts table) →
    read the savings balance. **Write flow:** `/member/<id>/sub-account/new` (open sub-account
    form) → confirmation screen with a generated account number. **Risky action:**
    `/member/<id>/transfer` (funds transfer) — gated behind an explicit confirmation step; the
    one action classified "risky" for the safety model (§3.4) to point at.
  - Legacy-styled on purpose: nested `<table>` layout, `name` attributes on form inputs (forms
    need them to submit), no `id`/`class`/`data-test` anywhere — the no-clean-DOM story from
    §3.7 is demonstrated by the surface itself, not just argued in prose.
  - Five fixed synthetic members, each engineered to hit one scenario: `M-10001` happy path,
    `M-10002` a frozen savings account, `M-10003` insufficient funds for a transfer, `M-10004`
    a restricted member (permission-denied on write), `M-10005` a closed member (not-found-style
    outcome).
  - A second, relabeled "Tenant B" configuration of the same app is planned (not yet built) to
    demonstrate cross-tenant reuse in `REPORT.md` §4 — same flow shape, different field
    labels/column order, so `tenant_overrides` gets exercised for real rather than only in prose.

## Locked decisions (continued)

- **Architecture: single process, one web dashboard app (`src/cua/app/`), not a CLI.**
  Implemented. A single Flask process (`python -m cua.app`, port 5100) is the primary,
  interactive interface — start a discovery run, watch its live screenshot, and (when the
  agent pauses for help) take over the session; `src/cua/artifact/`'s saved-capability
  browse/replay page is still to come once `src/cua/replay/` exists. Each run executes on its
  own background `threading.Thread`; the dashboard's Flask routes poll/relay into that thread's
  `EscalationBridge` concurrently — no second process, no queue. This supersedes the old
  standalone read-only viewer (`cua.agent.viewer`, deleted) outright, since the dashboard does
  everything it did plus real control transfer. The scriptable CLI (`cua.agent.cli`) is
  **kept**, not retired — it's the non-interactive entrypoint the `run-demo` skill uses for
  evidence capture, running with `interactive=False` so a `request_human_help` call with nobody
  watching times out immediately instead of hanging, rather than the primary way a person uses
  this system.
- **Human-in-the-loop control transfer: screenshot + click/type relay, not an `<iframe>`.**
  Implemented (`src/cua/escalation/bridge.py`). A literal `<iframe>` onto the target app's URL
  would load in the dashboard viewer's own browser, with its own fresh, unauthenticated cookie
  jar — completely disconnected from the Playwright session the agent is actually driving.
  Instead: the agent calls a new `request_human_help` tool (`kind`: `credential` / `captcha` /
  `confirmation` / `other`) when it needs one bounded thing done by a person, which blocks the
  discovery thread on an `EscalationBridge` (flips `control` to `"human"`, keeps the Playwright
  page open) until the dashboard calls `resume()`. While paused, the dashboard shows a live,
  polled, viewport-sized screenshot (not full-page — click coordinates are viewport-relative) and
  relays clicks/keystrokes straight onto the *same* live `Page` via `page.mouse.click` /
  `page.keyboard.type` — real control transfer onto the same session, not a mock of one. Every
  human action the bridge relays is appended to the run's trace with `actor: "human"`
  (`StepRecord.actor`), satisfying "record what the human did" (§3.6).
- **Opt-in encrypted credential store.** Implemented (`src/cua/escalation/credentials.py`).
  When `request_human_help(kind="credential", credential_name=...)` has no saved value, it
  pauses as above; if the human also fills in a "save as" name in the dashboard while sending
  the value, it's encrypted (Fernet, key from `CREDENTIAL_STORE_KEY` in `.env`, same pattern as
  `ANTHROPIC_API_KEY`) into its own table in `data/cua.db` and the raw value is never written to
  any trace/log — only its length is ever recorded (`EscalationBridge.relay_type`'s `sensitive`
  flag). A later run with the same `credential_name` resolves it automatically, without pausing
  at all. This is compatible with the hard constraint below ("never persist secrets... into
  artifacts or logs") because this store is neither of those — it's a separate, explicitly
  opt-in table, never embedded in an artifact and never echoed into a log.
- **Shared event memory (`src/cua/artifact/` + SQLite): a structural, cross-artifact
  `event_library` table, not a per-artifact-only outcome list.** Each artifact's
  `outcome_table` entry is backed by a row in a shared SQLite table keyed by a *structural*
  signature (`url_matches` / `text_contains` / `element_present` / `element_absent`) — never a
  semantic/embedding match, so nothing judgment-based ever enters the replay decision path
  (replay must never have an LLM, or anything LLM-like, deciding what an observed state means).
  Discovery is the one place an LLM may touch this store: when the discovery loop hits an event
  with no matching signature, the LLM drafts a candidate `event_library` entry
  (`status: pending_review`); a human must approve it (`status: approved`) before replay will
  ever consult it. This is also the concrete answer to "how does this generalize to any
  browser automation without an API": the signature vocabulary never references this target
  app, so an event learned on one artifact/app is available to any other artifact that hits the
  same structural pattern. SQLite, not a vector DB — a vector store would reintroduce exactly
  the non-determinism replay can't have. Scaling this (shared registry across tenants,
  per-tenant exceptions via `tenant_overrides`) is a documented design in `REPORT.md` §4/§7,
  not built now, per the hard constraint against building scaling infrastructure below.
- **Discovery agent (`src/cua/agent/`): Playwright (Python, sync API) + Claude tool-use loop,
  DOM-derived structured observation instead of screenshots.** Each step the agent gets a JSON
  list of visible interactive elements (link/button/input/select/textarea, with text/name/href)
  plus the page's visible text — the same information a human operator reads off the screen —
  rather than pixel coordinates. This is what still works with no clean DOM: no dependency on
  `id`/`class`/test attributes.
  - **Locator robustness is decided per element, at observation time**, in priority order:
    `id` attribute → `name` attribute → `href` (for links — more stable than visible text, which
    legacy table rows often repeat, e.g. "View Trust Center Page" on every row) → unique visible
    text (role + text) → CSS position among same-type elements as a last resort. This ranking is
    the "reasoning about robustness" the artifact schema (§3.2) will need to record, computed
    once here (`src/cua/agent/browser.py::_compute_locators`) and reused, not reconstructed later.
  - **Login is a deterministic bootstrap step, not part of the LLM loop.** A real capability
    assumes an already-authenticated session (auth/session management is a separate concern from
    task discovery), so the harness logs in before the agent starts reasoning about the goal.
  - **Tools exposed to the model:** `click`, `type_text`, `select_option`, `finish_success`
    (declares outputs), `request_human_help` (the bounded, expects-to-resume escalation --
    see the control-transfer decision above), `finish_stuck` (the terminal, gives-up
    escalation — no resume expected).
  - **A minimal, real allowlist (`src/cua/safety/allowlist.py`) is wired in now**, not deferred:
    every action is checked against an allowed origin + allowed action-type set before execution,
    so §3.4 isn't left as a total TODO while the rest of the system is built out.
  - Native JS `confirm()` dialogs (one of the target app's injected faults) are auto-accepted and
    logged, mirroring what a human operator would do, per the "recoverable conditions" language in
    §3.3.

## Open decisions

None outstanding — every `src/cua/*` subpackage is implemented (see below) and the full thread
(goal → discovery → recorded artifact → deterministic replay, happy path and two business
outcomes) has been run live against the real target app, not just unit-tested. Remaining work is
depth, not open design decisions; see `REPORT.md` §7 (Cuts) for what's intentionally thin.

- **Locator chains are real ranked fallbacks, computed and resolved identically everywhere.**
  `cua.agent.browser._compute_locator_chains` builds every applicable tier per element (`id` →
  `name` → unique `href` → unique text → `css_nth`, always last), not just the single best one.
  `cua.artifact.schema.resolve_locator_chain` is the one function that *resolves* a chain against
  a live page — discovery's `act_click`/`act_type`/`act_select` and replay's executor both call
  it, so "robust enough to replay" means the same thing in both places. A fallback tier
  succeeding (`tier_used > 0`) is recorded as drift, not silently ignored.
- **The artifact recorder (`cua.artifact.recorder.record_artifact`) is mechanical and
  explicit, not inferred.** The caller supplies `bindings` — a map of `param_name -> the
  concrete value used during this specific discovery run` (e.g. `member_id="M-10001"`) — and the
  recorder finds every occurrence of those values inside locator values and typed/selected values
  and replaces them with `{param_name}` placeholders. A typed/selected value that *exactly*
  equals a bound value becomes an `input_binding`, not a masked literal. No value is ever
  guessed at being a parameter; a wrong guess would be worse than requiring one reviewable list
  of bindings up front. The artifact's `checkpoint` and `outcome_table` are likewise
  caller-supplied, not auto-extracted from "whatever text looked distinctive" — see `REPORT.md`
  §7 for why a per-step `post_condition` (schema-supported) is left `None` by this recorder by
  design, not a silent gap.
- **Output extraction is a closed, deterministic vocabulary — never scraping code.**
  `OutputField.extract: Extraction` is either `label_value` (find a line of visible text
  containing a label, take what follows it on that line — exactly how this app's confirmation
  screens render "Transfer ID⟶XFR-233353") or `regex` (one capture group, for values embedded in
  a sentence). Implemented and tested in `cua.replay.executor._extract_one`.
- **One shared `condition_holds(condition, page_text, url, element_descriptors)`
  (`cua.artifact.schema`) evaluates every `Condition` everywhere one is checked** — a `Step`'s
  optional `post_condition`, an artifact's final `checkpoint`, its local `outcome_table`, and the
  shared `event_library`'s `match_event` (`cua.artifact.store`, refactored to call this instead
  of its own copy). One evaluator for the closed vocabulary, not reimplemented per caller.
- **Replay's outcome check runs before *and* after every step's action**, and always before
  declaring a locator-exhaustion or action failure a hard failure — a known business/recoverable
  signature always wins over "couldn't find the element" or "the action errored," since the page
  that caused either of those might just as well be a legitimate outcome, not a bug. Verified
  live: replaying the same artifact with a frozen account's ID correctly returns
  `business_outcome("account_frozen")`, not a failure.
- **Recoverable outcomes get one bounded, specific recovery attempt, not open-ended retry
  logic.** v1 (`cua.replay.executor._attempt_recovery`) handles exactly the one case that needed
  new code — a session-timeout page, recovered by logging back in once — because dialog
  auto-accept already happens for free at the `BrowserSession` level (both discovery and replay
  reuse it). Anything else classified `recoverable` falls through to the next check unchanged,
  an honest "detected but not auto-recovered" result rather than invented behavior (`REPORT.md`
  §7).
- **`src/cua/replay/` never imports the LLM or the discovery loop — enforced by a test, not
  just a comment.** `tests/test_replay.py::test_replay_package_never_imports_the_llm_or_discovery_loop`
  statically walks every file's import graph and fails the suite if that ever stops being true.

## Hard constraints (do not drift from these)

- **The discovery run must be real.** At least one genuine LLM-driven run against a live
  surface, with evidence saved under `/evidence/`. A described or simulated run does not
  satisfy this — it is the one thing in the brief that isn't a design choice.
  **Satisfied, twice over:** `evidence/discovery-run-20261023T034101Z-c6ba97/` (first live run,
  pre-locator-chain) and `evidence/discovery-run-for-artifact/` (the one actually recorded into
  the `cu-transfer-funds` artifact below) — both real, both `finish_success` with correct
  extracted outputs, full `trace.json` + one screenshot per step.
- **The full thread — discovery → artifact → deterministic replay, happy path and error
  path — must be real too, not just each piece in isolation.** **Satisfied:** the
  `cu-transfer-funds` artifact was recorded from `discovery-run-for-artifact/`'s real trace and
  saved to `data/cua.db`; `evidence/replay-happy-20261023/` replays it with a *different* amount
  ($25, not the recorded $50) to prove genuine parameterization, zero drift, correct outputs;
  `evidence/replay-error-20261023/` (frozen account) and
  `evidence/replay-error-insufficient-funds-20261023/` each replay it with inputs that trigger a
  real business outcome — both return a clean `business_outcome` result, not a crash or a false
  success.
- **Never persist secrets or raw sensitive data** (credentials, tokens, full PII) into artifacts or
  logs. Redact before writing anything to disk.
- **Don't build scaling infrastructure.** No queues, clusters, or real multi-tenant plumbing.
  Design the abstractions so they *could* extend that way (§3.7); building it is explicitly not
  rewarded and actively penalized as scope creep.
- **`/REPORT.md` must use exactly these seven headings, in this order:** Architecture,
  Artifact schema, Determinism & error handling, Heterogeneity & multi-tenant, Escalation &
  handoff, Safety, Cuts.
- **`/README.md` must include an exact runnable demo path**: the literal command(s) to run
  the agent on a goal, then replay the resulting artifact.
- Prefer a thin-but-real version of every core requirement over a polished subset of them. If
  something is cut, cut it at a clean, documented seam (state it in REPORT.md §7), don't leave a
  silent gap.

## Directory layout

```
.
├── CLAUDE.md              # this file
├── README.md              # setup + exact demo commands (assignment deliverable)
├── REPORT.md              # design write-up, 7 required headings (assignment deliverable)
├── docs/
│   └── assignment.md      # full brief, verbatim — source of truth
├── evidence/               # discovery-run + replay-run evidence (assignment deliverable)
├── data/                   # cua.db: SQLite store for artifacts + the shared event_library
├── src/cua/
│   ├── app/                # the web dashboard: run / artifacts+replay / escalation pages
│   ├── agent/              # LLM-driven observe/decide/act discovery loop
│   ├── artifact/           # artifact schema, serialization, versioning, event_library (SQLite)
│   ├── replay/             # deterministic replay executor, locator/error handling
│   ├── safety/             # allowlist enforcement, risk classification, redaction
│   ├── escalation/         # human-in-the-loop pause/handoff/resume mechanism
│   └── target_app/         # mock credit-union servicing console (target application)
├── tests/
├── .claude/
│   ├── agents/              # project-scoped subagents, see below
│   └── skills/run-demo/    # scripted end-to-end demo + evidence capture
└── pyproject.toml
```

Every subpackage is implemented: `src/cua/target_app/`, `src/cua/agent/` (discovery, now
emitting a real per-step `locator_chain` — see Open decisions below), `src/cua/safety/`,
`src/cua/artifact/` (schema + recorder + SQLite-backed store, including the shared
`event_library`), `src/cua/replay/` (the deterministic executor), `src/cua/escalation/`
(pause/relay/resume bridge + encrypted credential store), and `src/cua/app/` (the dashboard).

## Project subagents

- **`target-app-builder`** — builds/iterates on the mock target app, isolated from the core
  automation system.
- **`artifact-schema-reviewer`** — read-only; checks the artifact schema against the brief's
  explicit contract (steps, locator + robustness reasoning, typed inputs/outputs, checkpoint,
  versioning, reviewability).
- **`compliance-auditor`** — read-only; audits the whole repo against the assignment's exact
  deliverables and evaluation criteria before the submission is considered done.

## Workflow notes

- When a "your call" decision (§4 of the brief) gets made or changed, update both this file
  (Locked/Open decisions) and the relevant `REPORT.md` section in the same change — don't let
  them diverge.
- Evidence collection is scripted via the `run-demo` skill, not ad hoc — extend that skill's
  commands as CLI entrypoints are built, rather than capturing evidence by hand each time.
