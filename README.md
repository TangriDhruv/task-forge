# Computer-Use Automation System

A take-home submission for interface.ai: an LLM-driven "computer use" agent that discovers how
to accomplish a goal in a legacy back-office application, records the successful run as a
reusable, typed artifact, and replays that artifact deterministically (no LLM in the loop) on
demand — with human escalation and safety guardrails throughout.

See [REPORT.md](REPORT.md) for the design write-up and [docs/assignment.md](docs/assignment.md)
for the original brief this repo responds to. See [CLAUDE.md](CLAUDE.md) for the full log of
locked decisions and why each was made.

## Setup

```bash
git clone <this repo> && cd interface-ai-computer-use
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium   # one-time, if not already installed

cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY (and ANTHROPIC_WORKSPACE_ID, only if your
# key isn't itself scoped to a workspace -- see .env.example for how to tell)
```

No live services beyond what you start yourself (the mock target app, below) — there's nothing
to run "without" since the discovery agent needs a live surface to actually drive, by design.

## Demo path

Five real commands: start the target app, run discovery on a goal, record the resulting
artifact, then replay it twice — once with different inputs than discovery used (proving real
parameterization), once with inputs that trigger a genuine business outcome (proving replay
doesn't crash or silently succeed on a known-bad case).

```bash
# 1. Start the target app (the mock credit-union servicing console)
PYTHONPATH=src .venv/bin/python -m cua.target_app &

# 2. Run the discovery agent on a goal against the live app
PYTHONPATH=src .venv/bin/python -m cua.agent \
  --goal "Search for member M-10001, view their accounts, and transfer \$50 from checking to savings." \
  --target http://127.0.0.1:5055 \
  --output-dir evidence/discovery-run-demo

# 3. Record that successful run as a saved, reusable artifact
PYTHONPATH=src .venv/bin/python scripts/record_cu_transfer_artifact.py evidence/discovery-run-demo

# 4. Replay it deterministically -- happy path, with a DIFFERENT amount than discovery used
PYTHONPATH=src .venv/bin/python -m cua.replay \
  --artifact-id cu-transfer-funds \
  --input member_id=M-10001 --input from_account=CHK-10001-01 \
  --input to_account=SAV-10001-02 --input amount=25 \
  --target http://127.0.0.1:5055 --output-dir evidence/replay-happy-demo

# 5. Replay it again -- a real business outcome (M-10002's savings account is frozen)
PYTHONPATH=src .venv/bin/python -m cua.replay \
  --artifact-id cu-transfer-funds \
  --input member_id=M-10002 --input from_account=SAV-10002-02 \
  --input to_account=CHK-10002-01 --input amount=10 \
  --target http://127.0.0.1:5055 --output-dir evidence/replay-error-demo
```

Expect, respectively: `Status: success` with an `Outputs` block (step 2); each recorded step's
locator chain printed for review (step 3); `Status: success` + `Drift: []` (step 4 — zero
fallback tiers needed); `Status: business_outcome`, `Outcome: account_frozen` (step 5 — not a
crash, not a false success).

**Interactive use, with live screenshot + human escalation:** instead of step 2's CLI,
```bash
PYTHONPATH=src .venv/bin/python -m cua.app   # open http://127.0.0.1:5100
```
The dashboard runs discovery with a live, polled screenshot; when the agent calls
`request_human_help` (a password it doesn't have, a CAPTCHA, a risky confirmation), it pauses
on the *same* live session and the dashboard lets you click/type directly into it, including
optionally saving a credential (encrypted, `data/cua.db`) for next time.

## Evidence

Discovery-run and replay-run evidence lives under [evidence/](evidence/) — a saved artifact,
discovery-run logs (`trace.json` + one screenshot per step), a happy-path replay, and two
different error/exceptional-state replays (`account_frozen`, `insufficient_funds`), all from
real runs against the live target app, not simulated.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```
