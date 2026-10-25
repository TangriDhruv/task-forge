"""The observe -> decide -> act loop itself (assignment.md §3.1, §3.6).

Stopping conditions: the model calls finish_success or finish_stuck, the step
budget runs out, or an action would violate the allowlist policy. Every path
returns a RunResult carrying enough context (status, reason, trace, and a
screenshot when the run didn't cleanly succeed) to either report success or -
for "stuck"/"policy_violation"/"max_steps" - drive a human-escalation request
per §3.6. The actual "hand a human the live session" mechanism is a separate
module (src/cua/escalation/, not yet built); this loop is the seam that
detects "stuck" and stops safely rather than thrashing or guessing.

When `output_dir` is given, a screenshot is captured and `trace.json` is
rewritten (atomically) after *every* step, not just at the end - that is what
lets `cua.agent.viewer` poll the same directory and show the run live, and it
doubles as richer per-step evidence (§3.5), not only a signal on failure.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Callable
from urllib.parse import urlparse

from cua.agent.browser import ActionError, BrowserSession, Observation
from cua.agent.llm import Decider
from cua.agent.router import route_target
from cua.agent.trace import RunTrace, StepRecord
from cua.escalation import credentials
from cua.escalation.bridge import EscalationBridge, EscalationRequest
from cua.safety.allowlist import AllowlistPolicy, AllowlistViolation

DEFAULT_MAX_STEPS = 15
# How long request_human_help blocks when no dashboard is attached to service
# it (e.g. a scripted/CLI run) -- long enough that a bridge.resume() racing in
# right as the request lands still gets picked up, short enough that an
# unattended run doesn't hang; see loop._handle_human_help.
NON_INTERACTIVE_HELP_TIMEOUT = 0.1


@dataclass
class RunResult:
    status: str  # "success" | "stuck" | "max_steps" | "policy_violation"
    outputs: dict | None
    summary: str | None
    trace: RunTrace


def run_discovery(
    goal: str,
    base_url: str,
    start_path: str,
    username: str | None,
    password: str | None,
    max_steps: int = DEFAULT_MAX_STEPS,
    headless: bool = True,
    output_dir: str | None = None,
    bridge: EscalationBridge | None = None,
    interactive: bool = True,
    on_bridge_ready: Callable[[EscalationBridge], None] | None = None,
) -> RunResult:
    """``bridge`` lets a caller (the dashboard) hold onto the same
    EscalationBridge this run creates, so its Flask routes can poll/relay
    into the live session while this function blocks inside
    ``request_human_help`` handling. ``interactive=False`` (e.g. a scripted
    CLI run with nobody watching) makes any human-help request time out
    almost immediately and fall through to a "stuck" result instead of
    hanging forever. ``on_bridge_ready`` is called once, right after the
    bridge is created (which needs a live Playwright page, so it can't exist
    before ``browser.start()``) -- the dashboard uses it to publish the
    bridge into its run registry without the caller needing to construct
    one itself."""
    policy = AllowlistPolicy(allowed_origin=base_url)
    browser = BrowserSession(policy=policy, headless=headless)
    decider = Decider(goal=goal)
    trace = RunTrace(goal=goal, base_url=base_url)
    trace_path = os.path.join(output_dir, "trace.json") if output_dir else None

    def record_step(step_num: int, tool_name: str, tool_input: dict, assistant_text, action_error=None, locator_chain=None):
        screenshot_rel = _step_screenshot(browser, output_dir, step_num)
        trace.steps.append(
            StepRecord(
                step=step_num,
                observation=observation.to_prompt_dict(),
                tool_name=tool_name,
                tool_input=tool_input,
                assistant_text=assistant_text,
                action_error=action_error,
                screenshot=screenshot_rel,
                locator_chain=locator_chain,
            )
        )
        # Refreshed on every step (not just at the end) so trace.json always
        # reflects usage/cost so far, even if the run is later killed before
        # any return statement runs -- see cua.agent.usage.
        trace.usage = [asdict(r) for r in decider.usage_log]
        if trace_path:
            trace.save(trace_path)

    browser.start()
    if bridge is None:
        bridge = EscalationBridge(browser.page)
    if on_bridge_ready:
        on_bridge_ready(bridge)
    try:
        browser.login(base_url, username, password)
        browser.goto(base_url + start_path)
        observation = browser.observe()
        decision = decider.start(observation.to_prompt_dict())

        step_num = 0
        while True:
            step_num += 1

            if decision.tool_name in ("finish_success", "finish_stuck"):
                record_step(step_num, decision.tool_name, decision.tool_input, decision.assistant_text)
                if decision.tool_name == "finish_success":
                    trace.status = "success"
                else:
                    trace.status = "stuck"
                    trace.stuck_reason = decision.tool_input.get("reason")
                if trace_path:
                    trace.save(trace_path)
                if decision.tool_name == "finish_success":
                    return RunResult(
                        "success", decision.tool_input.get("outputs"), decision.tool_input.get("summary"), trace
                    )
                return RunResult("stuck", None, decision.tool_input.get("reason"), trace)

            if decision.tool_name == "request_human_help":
                help_result, gave_up = _handle_human_help(
                    browser, bridge, observation, decision.tool_input, step_num, trace, trace_path, interactive
                )
                record_step(step_num, decision.tool_name, decision.tool_input, decision.assistant_text)
                if gave_up:
                    trace.status = "stuck"
                    trace.stuck_reason = help_result
                    if trace_path:
                        trace.save(trace_path)
                    return RunResult("stuck", None, help_result, trace)

                if step_num >= max_steps:
                    trace.status = "max_steps"
                    if trace_path:
                        trace.save(trace_path)
                    return RunResult(
                        "max_steps", None, f"Stopped after {max_steps} steps without finishing.", trace
                    )

                observation = browser.observe()
                result_dict = observation.to_prompt_dict()
                result_dict["human_help_result"] = help_result
                decision = decider.respond(decision.tool_use_id, result_dict)
                continue

            error_text = None
            acted_locator_chain = None
            try:
                acted_locator_chain = _execute(browser, observation, decision.tool_name, decision.tool_input)
            except ActionError as exc:
                error_text = str(exc)
            except AllowlistViolation as exc:
                record_step(step_num, decision.tool_name, decision.tool_input, decision.assistant_text, str(exc))
                trace.status = "policy_violation"
                if trace_path:
                    trace.save(trace_path)
                return RunResult("policy_violation", None, str(exc), trace)

            record_step(
                step_num, decision.tool_name, decision.tool_input, decision.assistant_text, error_text,
                locator_chain=acted_locator_chain,
            )

            if step_num >= max_steps:
                trace.status = "max_steps"
                if trace_path:
                    trace.save(trace_path)
                return RunResult(
                    "max_steps", None, f"Stopped after {max_steps} steps without finishing.", trace
                )

            observation = browser.observe()
            result_dict = observation.to_prompt_dict()
            if error_text:
                result_dict["action_error"] = error_text
            decision = decider.respond(decision.tool_use_id, result_dict)
    finally:
        browser.close()


def run_task(
    goal: str,
    known_targets: dict[str, str] | None = None,
    username: str | None = None,
    password: str | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    headless: bool = True,
    output_dir: str | None = None,
    bridge: EscalationBridge | None = None,
    interactive: bool = True,
    on_bridge_ready: Callable[[EscalationBridge], None] | None = None,
) -> RunResult:
    """Goal-only entry point: no ``--target`` required. Resolves which site
    to use via ``cua.agent.router.route_target`` *before* any browser or
    allowlist exists -- see router.py's module docstring for why that
    ordering is what keeps the allowlist's "one locked origin for the whole
    run" guarantee intact even though the origin is now chosen by the model
    rather than a human. ``known_targets`` maps a short description to a
    base URL (e.g. the CU Servicing Console); omit it to always resolve via
    web search. ``username``/``password`` default to ``None`` -- a routed
    external site is assumed to need no login unless you supply one (e.g.
    for a known local target that does)."""
    routing = route_target(goal, known_targets)
    parsed = urlparse(routing.url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    start_path = parsed.path or "/"

    result = run_discovery(
        goal=goal,
        base_url=base_url,
        start_path=start_path,
        username=username,
        password=password,
        max_steps=max_steps,
        headless=headless,
        output_dir=output_dir,
        bridge=bridge,
        interactive=interactive,
        on_bridge_ready=on_bridge_ready,
    )
    result.trace.routing = {"url": routing.url, "reasoning": routing.reasoning, "source": routing.source}
    # Routing's own API calls happened before run_discovery's trace existed
    # -- prepend them so this run's trace.usage covers the whole thing, not
    # just the discovery half.
    result.trace.usage = routing.usage + result.trace.usage
    if output_dir:
        result.trace.save(os.path.join(output_dir, "trace.json"))
    return result


def _handle_human_help(
    browser: BrowserSession,
    bridge: EscalationBridge,
    observation: Observation,
    tool_input: dict,
    step_num: int,
    trace: RunTrace,
    trace_path: str | None,
    interactive: bool,
) -> tuple[str, bool]:
    """Returns (result_text, gave_up). ``gave_up=True`` means nobody was able
    to help (non-interactive run, or the human never resumed) and the run
    should end as "stuck" -- same terminal shape as finish_stuck, just
    reached via a different tool."""
    kind = tool_input.get("kind", "other")
    reason = tool_input.get("reason", "")
    field_ref = tool_input.get("field_ref")
    credential_name = tool_input.get("credential_name")

    # Known credential: resolve silently, no human pause at all. This is the
    # whole point of naming credentials -- the first encounter pauses for a
    # human, every later one doesn't.
    if kind == "credential" and credential_name and credentials.has_credential(credential_name):
        ref = field_ref
        element = observation.elements[ref] if isinstance(ref, int) and 0 <= ref < len(observation.elements) else None
        if element is not None:
            value = credentials.get_credential(credential_name)
            browser.act_type(element, value, submit=False)
            return f"resolved automatically using saved credential '{credential_name}'", False

    request = EscalationRequest(
        kind=kind, reason=reason, step=step_num, field_ref=field_ref, credential_name=credential_name
    )
    trace.status = "paused_for_human"
    if trace_path:
        trace.save(trace_path)

    timeout = None if interactive else NON_INTERACTIVE_HELP_TIMEOUT
    actions = bridge.request_human(request, timeout=timeout)

    trace.status = "running"
    if not actions:
        return f"no human was available to help ({kind}): {reason}", True

    for action in actions:
        trace.steps.append(
            StepRecord(
                step=step_num,
                observation={},
                tool_name=f"human_{action.kind}",
                tool_input=action.detail,
                assistant_text=None,
                actor="human",
            )
        )
    return f"a human performed {len(actions)} action(s) and signaled done", False


def _execute(browser: BrowserSession, observation: Observation, tool_name: str, tool_input: dict) -> list[dict] | None:
    """Returns the acted-upon element's locator chain, serialized as plain
    dicts (for StepRecord.locator_chain -- see trace.py), or None for an
    unrecognized tool. Raises ActionError before touching the browser at all
    if the ref doesn't resolve, so a trace never records a chain for an
    action that didn't actually happen."""
    ref = tool_input.get("ref")
    element = (
        observation.elements[ref] if isinstance(ref, int) and 0 <= ref < len(observation.elements) else None
    )
    if tool_name in ("click", "type_text", "select_option") and element is None:
        raise ActionError(
            f"ref {ref!r} does not exist in the current observation ({len(observation.elements)} elements)"
        )

    if tool_name == "click":
        browser.act_click(element)
    elif tool_name == "type_text":
        browser.act_type(element, tool_input.get("text", ""), submit=bool(tool_input.get("submit", False)))
    elif tool_name == "select_option":
        browser.act_select(element, tool_input.get("value", ""))
    else:
        raise ActionError(f"unknown tool: {tool_name}")

    return [
        {"by": l.by, "value": l.value, "tag": l.tag, "nth": l.nth} for l in element.locator_chain
    ]


def _step_screenshot(browser: BrowserSession, output_dir: str | None, step_num: int) -> str | None:
    if not output_dir:
        return None
    shots_dir = os.path.join(output_dir, "screenshots")
    os.makedirs(shots_dir, exist_ok=True)
    rel_path = os.path.join("screenshots", f"step_{step_num:03d}.png")
    try:
        browser.screenshot(os.path.join(output_dir, rel_path))
    except Exception:  # noqa: BLE001 - evidence capture must never crash the run
        return None
    return rel_path
