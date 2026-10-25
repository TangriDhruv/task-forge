---
name: run-demo
description: Run the full end-to-end demo this assignment requires — start the target app, run the discovery agent on a goal, replay the saved artifact (happy path), replay it again to trigger an error/exceptional state, and collect all evidence into /evidence/. Use when asked to "run the demo," "generate evidence," or "regenerate /evidence/."
---

# Run demo

Scripts the exact end-to-end path `docs/assignment.md` §6.3 asks for, so evidence generation is
a repeatable command rather than something captured by hand each time. Every command below is
real and has been run successfully — see `evidence/` for the actual output.

## Preconditions

- `.env` has `ANTHROPIC_API_KEY` set, and (only if your key isn't itself workspace-scoped)
  `ANTHROPIC_WORKSPACE_ID` — see `.env.example`.
- Dependencies installed: `pip install -e ".[dev]"` (installs Playwright's Python bindings;
  run `playwright install chromium` once if browsers aren't already installed).

## Steps

1. **Start the target app.**
   ```
   PYTHONPATH=src .venv/bin/python -m cua.target_app &
   ```
   Default: `http://127.0.0.1:5055`. Record the PID to tear down in step 7.

2. **Run the discovery agent on a goal.**
   ```
   PYTHONPATH=src .venv/bin/python -m cua.agent \
     --goal "Search for member M-10001, view their accounts, and transfer \$50 from checking to savings." \
     --target http://127.0.0.1:5055 \
     --output-dir evidence/discovery-run-demo
   ```
   - Writes `trace.json` (every step, including the real `locator_chain` used) and one
     screenshot per step into `evidence/discovery-run-demo/`.
   - Confirm it prints `Status: success` with an `Outputs` block (ticket number, accounts,
     amount, status).
   - This CLI path runs with `interactive=False`: a `request_human_help` call with nobody
     watching times out almost immediately rather than hanging. For a goal expected to need a
     human mid-flow (password/CAPTCHA/confirmation), use the interactive dashboard instead:
     `PYTHONPATH=src .venv/bin/python -m cua.app`, then open `http://127.0.0.1:5100`.

3. **Record the trace into a saved artifact.**
   ```
   PYTHONPATH=src .venv/bin/python scripts/record_cu_transfer_artifact.py evidence/discovery-run-demo
   ```
   - Saves the `cu-transfer-funds` capability to `data/cua.db`, printing each recorded step and
     its locator chain for review.
   - This script is deliberately specific to this one capability, not a generic "record any
     trace" CLI — see `cua.artifact.recorder`'s module docstring for why the parameter bindings,
     checkpoint, and outcome table are explicit and reviewable here rather than inferred.

4. **Replay the artifact — happy path, with different inputs than the recording used.**
   ```
   PYTHONPATH=src .venv/bin/python -m cua.replay \
     --artifact-id cu-transfer-funds \
     --input member_id=M-10001 --input from_account=CHK-10001-01 \
     --input to_account=SAV-10001-02 --input amount=25 \
     --target http://127.0.0.1:5055 \
     --output-dir evidence/replay-happy-demo
   ```
   - `amount=25` here vs. `$50` during discovery deliberately proves this is genuine
     parameterization, not a baked-in recording.
   - Confirm `Status: success` with the declared outputs, and `Drift: []` (every locator
     resolved at its primary tier — no fallback needed).

5. **Replay the artifact again — a real business outcome, no crash.**
   ```
   PYTHONPATH=src .venv/bin/python -m cua.replay \
     --artifact-id cu-transfer-funds \
     --input member_id=M-10002 --input from_account=SAV-10002-02 \
     --input to_account=CHK-10002-01 --input amount=10 \
     --target http://127.0.0.1:5055 \
     --output-dir evidence/replay-error-demo
   ```
   - `M-10002`'s savings account is seeded Frozen (see `src/cua/target_app/data.py`) — confirm
     `Status: business_outcome`, `Outcome: account_frozen`, not a failure or a false success.
   - A second, equally real outcome worth demonstrating: `member_id=M-10003`,
     `from_account=CHK-10003-01`, `amount=500` (balance is only $42.10) → `insufficient_funds`.

6. **Tear down** the target app process started in step 1.

7. **Sanity-check `/evidence/`** against `docs/assignment.md` §6.3: a saved artifact (query
   `data/cua.db`'s `artifacts` table, or re-run step 3 and read its printed step list),
   discovery-run logs, a happy-path replay, and an error-case replay should all be present and
   readable. Run the `compliance-auditor` subagent afterward for a second check against the full
   deliverables list, not just this skill's own output.
