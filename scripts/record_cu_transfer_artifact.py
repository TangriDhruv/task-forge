#!/usr/bin/env python3
"""Record the "transfer funds" capability from a successful discovery
trace, and save it as the ``cu-transfer-funds`` artifact.

Usage:
    .venv/bin/python scripts/record_cu_transfer_artifact.py <trace_dir>

``<trace_dir>`` is a discovery run's output directory (containing
``trace.json``) for the goal: "Search for member M-10001, view their
accounts, and transfer $50 from checking to savings." (or any equivalent
goal using those same concrete values -- see ``BINDINGS`` below).

This is deliberately a committed, reviewable script rather than a generic
"record any trace" CLI: the rich structure an artifact needs (parameter
bindings, output extraction rules, the checkpoint, the outcome table) is
meant to be curated by whoever is promoting a trace into a trusted,
reusable capability -- see cua.artifact.recorder's module docstring for why
that's explicit here rather than inferred.
"""

from __future__ import annotations

import json
import sys

from cua.agent.trace import RunTrace, StepRecord
from cua.artifact import (
    Condition,
    Extraction,
    InputParam,
    OutcomeRule,
    OutputField,
    record_artifact,
    save_artifact,
)

# The concrete values this capability's discovery run actually used -- see
# cua.artifact.recorder: these get masked into {param} placeholders
# throughout the recorded steps, checkpoint, and outcome table.
BINDINGS = {
    "member_id": "M-10001",
    "from_account": "CHK-10001-01",
    "to_account": "SAV-10001-02",
    "amount": "50",
}

OUTCOME_TABLE = [
    OutcomeRule(Condition(kind="text_contains", value="is closed and cannot"), "business", "member_closed"),
    OutcomeRule(Condition(kind="text_contains", value="ERROR:"), "business", "member_not_found"),
    OutcomeRule(Condition(kind="text_contains", value="FROZEN ACCOUNT"), "business", "account_frozen"),
    OutcomeRule(Condition(kind="text_contains", value="INSUFFICIENT FUNDS"), "business", "insufficient_funds"),
    OutcomeRule(Condition(kind="text_contains", value="ACCESS DENIED"), "business", "permission_denied"),
    OutcomeRule(Condition(kind="text_contains", value="SESSION TIMEOUT"), "recoverable", "session_timeout"),
    OutcomeRule(Condition(kind="text_contains", value="Error Reference:"), "hard_failure", "server_error"),
]


def _load_trace(path: str) -> RunTrace:
    with open(path) as f:
        d = json.load(f)
    trace = RunTrace(
        goal=d["goal"], base_url=d["base_url"], started_at=d["started_at"],
        status=d["status"], stuck_reason=d.get("stuck_reason"),
    )
    trace.steps = [
        StepRecord(
            step=s["step"], observation=s["observation"], tool_name=s["tool_name"],
            tool_input=s["tool_input"], assistant_text=s.get("assistant_text"),
            action_error=s.get("action_error"), screenshot=s.get("screenshot"),
            locator_chain=s.get("locator_chain"), actor=s.get("actor", "agent"),
        )
        for s in d["steps"]
    ]
    return trace


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <discovery-run-dir>", file=sys.stderr)
        return 2
    trace_path = f"{sys.argv[1].rstrip('/')}/trace.json"
    trace = _load_trace(trace_path)

    artifact = record_artifact(
        trace,
        artifact_id="cu-transfer-funds",
        name="Transfer funds between a member's accounts",
        goal_description=(
            "Search for a member, open the transfer form, and move funds between two of "
            "their accounts."
        ),
        app_id="cu-servicing-console",
        base_url_pattern="http://127.0.0.1:5055",
        inputs=[
            InputParam(name="member_id", type="string", description="e.g. M-10001"),
            InputParam(name="from_account", type="string", description="Source account ID"),
            InputParam(name="to_account", type="string", description="Destination account ID"),
            InputParam(name="amount", type="number", description="Amount to transfer"),
        ],
        outputs=[
            OutputField(name="transfer_id", type="string", extract=Extraction("label_value", "Transfer ID")),
            OutputField(name="from_account", type="string", extract=Extraction("label_value", "From Account")),
            OutputField(name="to_account", type="string", extract=Extraction("label_value", "To Account")),
            OutputField(name="amount", type="number", extract=Extraction("label_value", "Amount")),
            OutputField(name="status", type="string", extract=Extraction("label_value", "Status")),
        ],
        bindings=BINDINGS,
        checkpoint=Condition(kind="text_contains", value="TRANSFER COMPLETED"),
        outcome_table=OUTCOME_TABLE,
        created_from_run=sys.argv[1],
    )

    save_artifact(artifact)
    print(f"Saved artifact '{artifact.artifact_id}' v{artifact.version} to data/cua.db")
    for step in artifact.steps:
        print(f"  {step.step_id}: {step.action_type} chain={[l.describe() for l in step.locator_chain]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
