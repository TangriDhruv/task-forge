"""Deterministic fault injection for the mock target app.

Two complementary mechanisms, both documented in the target app's README section:

1. **Per-request override** -- append ``?fault=<code>`` (or a same-named hidden
   form field) to any request. This forces that single request to hit the named
   fault regardless of the natural data/state, which is what a discovery run or a
   replay's error-path evidence capture wants: a deterministic, repeatable trigger.

2. **Global/session toggles** -- ``GET /admin/faults`` shows current config,
   ``POST /admin/faults`` updates it. Config is a plain in-memory dict (no
   external store), reset on process restart. Used for state that naturally
   spans multiple requests (session TTL) or that should apply probabilistically
   across *all* traffic (occasional slowness), rather than a single call site.

Fault codes (usable via ``?fault=``):

- ``not_found``        -- force a "record not found" business outcome.
- ``validation_error``  -- force the request-access form to fail validation.
- ``permission_denied`` -- force an access-denied business outcome.
- ``confirm_dialog``    -- force the native JS confirm() interstitial on the
                           request-access form, regardless of urgency.
- ``session_timeout``   -- force the current session to be treated as expired.
- ``slow``              -- add an artificial delay before responding.
- ``server_error``      -- force an unhandled 500 (a "failed load").
"""

from __future__ import annotations

import random
import time

FAULT_CODES = (
    "not_found",
    "validation_error",
    "permission_denied",
    "confirm_dialog",
    "session_timeout",
    "slow",
    "server_error",
)

# Global config, mutated only via the /admin/faults endpoint. Kept intentionally
# tiny -- this is a knob panel for demo/replay evidence capture, not a feature.
CONFIG = {
    # Session time-to-live, in seconds, before a logged-in session is treated as
    # expired. Small default (10 min) is realistic; set it very low (e.g. 5s)
    # via /admin/faults to reliably reproduce a mid-flow timeout in a demo run.
    "session_ttl_seconds": 600,
    # Probability (0.0-1.0) that ANY request gets an artificial delay, to model
    # "transient slowness" that happens occasionally rather than on-demand.
    "slow_mode_probability": 0.0,
    "slow_mode_delay_seconds": 3.0,
    # Force every applicable request to hit these outcomes, independent of
    # per-request ?fault= params. Off by default. Useful for demoing "the whole
    # app is degraded" scenarios from one switch instead of editing every URL.
    "force_permission_denied": False,
    "force_validation_error": False,
}

DEFAULT_CONFIG = dict(CONFIG)


def reset_config() -> None:
    CONFIG.update(DEFAULT_CONFIG)


def active_fault(args, form=None) -> str | None:
    """Return the fault code requested for this request, if any."""
    code = None
    if args is not None:
        code = args.get("fault")
    if not code and form is not None:
        code = form.get("fault")
    if code in FAULT_CODES:
        return code
    return None


def maybe_apply_slowness(fault: str | None) -> None:
    """Sleep if this request was explicitly or probabilistically marked slow."""
    if fault == "slow":
        time.sleep(CONFIG["slow_mode_delay_seconds"])
        return
    prob = CONFIG["slow_mode_probability"]
    if prob > 0 and random.random() < prob:
        time.sleep(CONFIG["slow_mode_delay_seconds"])
