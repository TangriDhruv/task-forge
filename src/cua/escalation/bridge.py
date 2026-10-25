"""Pause / control-transfer / resume mechanism for a live discovery (or
replay) session (assignment.md §3.6).

The core idea: the discovery loop runs on its own thread. When it needs a
human, it calls ``EscalationBridge.request_human(...)``, which flips
``control`` to ``"human"`` and blocks that thread on a ``threading.Event`` --
it does **not** close the browser or give up the session. Meanwhile the
dashboard (running on Flask's own request-handling threads, in the same
process -- see CLAUDE.md "Architecture: single process") polls
``screenshot_bytes()`` and relays clicks/keystrokes straight onto the live
Playwright ``Page`` via ``relay_click``/``relay_type``/``relay_key``. Calling
``resume()`` unblocks the discovery thread, which picks back up on whatever
page state the human left behind.

This is deliberately screenshot-plus-relay, not a literal ``<iframe>`` onto
the target app: an iframe would load in the *viewer's own* browser, with its
own separate, unauthenticated cookie jar, completely disconnected from the
Playwright session the agent is actually driving (see CLAUDE.md). Relaying
onto the live ``Page`` object is what keeps this "the same session," which
is the one thing assignment.md §3.6 is not negotiable about.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class HumanAction:
    kind: str  # "click" | "type" | "key"
    detail: dict
    at: str = field(default_factory=_now)


@dataclass
class EscalationRequest:
    kind: str  # "credential" | "captcha" | "confirmation" | "other"
    reason: str
    step: int
    field_ref: int | None = None
    credential_name: str | None = None


class EscalationBridge:
    """Wraps one live Playwright ``Page`` so a human can take it over without
    a fresh session ever being created."""

    def __init__(self, page):
        self._page = page
        self._control = "agent"
        self._pending: EscalationRequest | None = None
        self._resume_event = threading.Event()
        self._human_actions: list[HumanAction] = []
        self._lock = threading.Lock()

    @property
    def control(self) -> str:
        return self._control

    @property
    def pending(self) -> EscalationRequest | None:
        return self._pending

    @property
    def viewport_size(self) -> dict:
        return self._page.viewport_size or {"width": 1280, "height": 720}

    # -- called from the discovery/replay thread ---------------------------

    def request_human(self, request: EscalationRequest, timeout: float | None = None) -> list[HumanAction]:
        """Blocks the calling thread until ``resume()`` is called (or
        ``timeout`` elapses). Returns whatever actions the human performed
        while in control -- empty if nobody showed up (e.g. a non-interactive
        run with no dashboard attached, which passes a short timeout)."""
        with self._lock:
            self._control = "human"
            self._pending = request
            self._human_actions = []
            self._resume_event.clear()
        self._resume_event.wait(timeout)
        with self._lock:
            self._control = "agent"
            self._pending = None
            actions = list(self._human_actions)
            self._human_actions = []
        return actions

    # -- called from the dashboard's Flask routes ---------------------------

    def resume(self) -> None:
        self._resume_event.set()

    def screenshot_bytes(self) -> bytes:
        # Viewport-only (not full_page): click-relay coordinates are
        # viewport-relative, so the displayed image must match the viewport
        # exactly for the frontend's coordinate scaling to be correct.
        return self._page.screenshot(full_page=False)

    def relay_click(self, x: float, y: float) -> None:
        self._require_human_control()
        self._page.mouse.click(x, y)
        self._record("click", {"x": x, "y": y})

    def relay_type(self, text: str, sensitive: bool = False) -> None:
        self._require_human_control()
        self._page.keyboard.type(text)
        # Never record the literal text for a sensitive field (a password, a
        # saved credential) -- only its length. See CLAUDE.md: never persist
        # secrets into logs, and a trace is a log for this purpose.
        self._record("type", {"length": len(text)} if sensitive else {"text": text})

    def relay_key(self, key: str) -> None:
        self._require_human_control()
        self._page.keyboard.press(key)
        self._record("key", {"key": key})

    def _require_human_control(self) -> None:
        if self._control != "human":
            raise PermissionError("the agent is currently in control; no escalation is in progress")

    def _record(self, kind: str, detail: dict) -> None:
        with self._lock:
            self._human_actions.append(HumanAction(kind=kind, detail=detail))
