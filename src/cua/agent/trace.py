"""Structured trace of a discovery run: the evidence log for assignment.md §3.5,
and the raw material a future artifact-recording step (§3.2) would compress
into a reusable capability. Kept separate from the raw model conversation
(`Decider.messages`) by design — the brief asks for the artifact to be
"decoupled from the raw model transcript", and that separation starts here:
a StepRecord captures the observation, the decision, and the outcome, not the
full prompt/response wire format.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class StepRecord:
    step: int
    observation: dict
    tool_name: str
    tool_input: dict
    assistant_text: str | None = None
    action_error: str | None = None
    # Relative path (from the run's output dir) to a screenshot taken right
    # after this step, if one was captured — what the live viewer displays.
    screenshot: str | None = None
    # The *actual* ranked locator chain used for this step's element, serialized
    # as plain dicts ({"by", "value", "tag", "nth"}) -- captured at the moment
    # of action (cua.agent.loop._execute), not reconstructed afterwards from
    # this record's lossy `observation` (which only carries what the LLM
    # prompt needed: ref/role/text/name/href, never id/tag). This is what
    # `cua.artifact.recorder` reads to build each Step.locator_chain; None for
    # steps with no acted-upon element (finish_success, human_*, etc.).
    locator_chain: list[dict] | None = None
    # "agent" (default) or "human" -- set on the StepRecord(s) recorded for
    # actions a person took during an escalation pause (assignment.md §3.6:
    # "record what the human did"). tool_input for a human step must never
    # contain a raw secret value; see cua.escalation.bridge's redaction at
    # the point of capture.
    actor: str = "agent"


@dataclass
class RunTrace:
    goal: str
    base_url: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "running"
    stuck_reason: str | None = None
    steps: list[StepRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str) -> None:
        # Written after every step so a live viewer can poll this file while
        # the run is in progress; atomic replace so a concurrent reader never
        # sees a half-written file.
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        os.replace(tmp_path, path)
