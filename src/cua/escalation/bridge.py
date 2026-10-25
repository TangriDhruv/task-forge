"""Pause / control-transfer / resume mechanism for a live discovery (or
replay) session (assignment.md §3.6).

The core idea: the discovery loop runs on its own thread. When it needs a
human, it calls ``EscalationBridge.request_human(...)``, which flips
``control`` to ``"human"`` -- it does **not** close the browser or give up
the session. Meanwhile the dashboard (running on Flask's own
request-handling threads, in the same process -- see CLAUDE.md
"Architecture: single process") polls ``screenshot_bytes()`` and relays
clicks/keystrokes straight onto the live Playwright ``Page`` via
``relay_click``/``relay_type``/``relay_key``. Calling ``resume()`` unblocks
the discovery thread, which picks back up on whatever page state the human
left behind.

This is deliberately screenshot-plus-relay, not a literal ``<iframe>`` onto
the target app: an iframe would load in the *viewer's own* browser, with its
own separate, unauthenticated cookie jar, completely disconnected from the
Playwright session the agent is actually driving (see CLAUDE.md). Relaying
onto the live ``Page`` object is what keeps this "the same session," which
is the one thing assignment.md §3.6 is not negotiable about.

**Why this is a command queue, not direct calls from Flask's threads**:
Playwright's *sync* API is not thread-safe -- every call on a given
``Page`` must happen on the thread that created its browser session, or
Playwright's internal sync/async bridge raises
``greenlet.error: Cannot switch to a different thread``. Discovered live
(not in the unit tests, which use a fake page with no thread-affinity
constraint at all -- a real gap in what "verified" meant until this was
actually hit against a real browser): the dashboard's relay routes run on
Flask's own request threads, so calling ``self._page.keyboard.press(...)``
etc. directly from there is broken. The fix: ``relay_*``/``screenshot_bytes``
only ever *enqueue* a command and block for its result; ``request_human``'s
wait loop -- running on the correct, owning thread -- drains that queue and
is the only code that ever touches ``self._page`` directly.
"""

from __future__ import annotations

import queue
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
        # (kind, args, result_queue) -- see module docstring. Only drained
        # by the owning thread, inside request_human's wait loop below.
        self._commands: "queue.Queue[tuple[str, dict, queue.Queue]]" = queue.Queue()

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
        ``timeout`` elapses) -- but doesn't just sleep: it's the only loop
        that drains ``self._commands``, so this is also what makes
        screenshot/click/type/key relay actually work (see module
        docstring on thread affinity). Returns whatever actions the human
        performed while in control -- empty if nobody showed up (e.g. a
        non-interactive run with no dashboard attached, which passes a
        short timeout)."""
        with self._lock:
            self._control = "human"
            self._pending = request
            self._human_actions = []
            self._resume_event.clear()

        deadline = None if timeout is None else time.monotonic() + timeout
        poll_interval = 0.05
        while not self._resume_event.is_set():
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait_for = min(poll_interval, remaining)
            else:
                wait_for = poll_interval
            try:
                kind, args, result_q = self._commands.get(timeout=wait_for)
            except queue.Empty:
                continue
            try:
                result_q.put(("ok", self._dispatch(kind, args)))
            except Exception as exc:  # noqa: BLE001 -- propagated to the waiting Flask thread, not swallowed
                result_q.put(("error", exc))

        with self._lock:
            self._control = "agent"
            self._pending = None
            actions = list(self._human_actions)
            self._human_actions = []
        return actions

    def _dispatch(self, kind: str, args: dict):
        """Runs on the owning thread only -- the one place self._page is
        ever touched from inside a command."""
        if kind == "screenshot":
            # Viewport-only (not full_page): click-relay coordinates are
            # viewport-relative, so the displayed image must match the
            # viewport exactly for the frontend's coordinate scaling.
            return self._page.screenshot(full_page=False)
        if kind == "click":
            self._page.mouse.click(args["x"], args["y"])
            self._record("click", {"x": args["x"], "y": args["y"]})
            return None
        if kind == "type":
            self._page.keyboard.type(args["text"])
            # Never record the literal text for a sensitive field (a
            # password, a saved credential) -- only its length. See
            # CLAUDE.md: never persist secrets into logs, and a trace is a
            # log for this purpose.
            detail = {"length": len(args["text"])} if args.get("sensitive") else {"text": args["text"]}
            self._record("type", detail)
            return None
        if kind == "key":
            self._page.keyboard.press(args["key"])
            self._record("key", {"key": args["key"]})
            return None
        raise ValueError(f"unknown escalation command: {kind}")

    # -- called from the dashboard's Flask routes ---------------------------

    def resume(self) -> None:
        self._resume_event.set()

    def _submit(self, kind: str, args: dict, timeout: float = 10.0):
        if self._control != "human":
            raise PermissionError("the agent is currently in control; no escalation is in progress")
        result_q: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)
        self._commands.put((kind, args, result_q))
        try:
            status, value = result_q.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(
                f"escalation command {kind!r} timed out waiting for the owning thread to service it"
            ) from exc
        if status == "error":
            raise value
        return value

    def screenshot_bytes(self) -> bytes:
        return self._submit("screenshot", {})

    def relay_click(self, x: float, y: float) -> None:
        self._submit("click", {"x": x, "y": y})

    def relay_type(self, text: str, sensitive: bool = False) -> None:
        self._submit("type", {"text": text, "sensitive": sensitive})

    def relay_key(self, key: str) -> None:
        self._submit("key", {"key": key})

    def _record(self, kind: str, detail: dict) -> None:
        with self._lock:
            self._human_actions.append(HumanAction(kind=kind, detail=detail))
