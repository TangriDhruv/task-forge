"""Allowlist enforcement for agent actions (assignment.md §3.4).

Deliberately minimal: a real deployment would load per-tenant allowlists from
config, but the shape here — a base URL plus a set of permitted action types —
is the seam that would extend to that. Nothing in the agent loop is allowed to
navigate off the allowed origin or perform an action type not in the allowed
set; violations are raised, never silently downgraded, so a policy breach
surfaces as a hard failure instead of a quietly-wrong run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

# Actions that only read state. Kept separate from write-capable actions so a
# future "confirm risky actions" gate (assignment.md §3.4) has something to
# key off — request_access_submit is the one write-capable action our target
# app exposes today.
SAFE_ACTION_TYPES = frozenset({"click", "type_text", "select_option", "observe"})
RISKY_ACTION_TYPES = frozenset({"submit_form"})


class AllowlistViolation(RuntimeError):
    """Raised when the agent (or a replay) attempts an out-of-policy action."""


@dataclass(frozen=True)
class AllowlistPolicy:
    allowed_origin: str
    allowed_action_types: frozenset[str] = field(
        default_factory=lambda: SAFE_ACTION_TYPES | RISKY_ACTION_TYPES
    )

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin != self.allowed_origin:
            raise AllowlistViolation(
                f"URL '{url}' is outside the allowed origin '{self.allowed_origin}'"
            )

    def check_action_type(self, action_type: str) -> None:
        if action_type not in self.allowed_action_types:
            raise AllowlistViolation(
                f"Action type '{action_type}' is not in the allowed set "
                f"{sorted(self.allowed_action_types)}"
            )

    def is_risky(self, action_type: str) -> bool:
        return action_type in RISKY_ACTION_TYPES
