"""Deterministic replay (assignment.md §3.3): given a saved ``Artifact`` and
a set of input parameters, walk its fixed step list and report success,
outputs, a known business outcome, or a clear, debuggable failure -- with
**no LLM in the decision loop** at any point. Nothing in this module
imports ``cua.agent.llm`` or ``cua.agent.loop``; that's enforced by
``tests/test_replay.py``'s import-graph check, not just a comment.

Every "decision" replay makes was already made once, during discovery, and
is simply being executed:
  - *which* step runs next: the artifact's fixed ``steps`` list, in order.
  - *how* to find this step's target: ``step.locator_chain``, tried top to
    bottom via the same ``resolve_locator_chain`` discovery itself uses
    (see ``cua.artifact.schema``) -- a fallback tier succeeding is recorded
    as drift, not silently ignored.
  - *what a page state means*: the artifact's own ``outcome_table`` plus the
    shared, cross-artifact ``event_library`` (``cua.artifact.store``,
    **approved rows only**) -- a fixed signature -> outcome mapping, never a
    fresh judgment call.

Checked in that order at every step and at the end: known outcome first
(a business/recoverable/hard_failure match short-circuits everything else),
then the step's own ``post_condition`` (if it declared one), then -- after
the last step -- the artifact's overall ``checkpoint``. Outputs are only
ever extracted after the checkpoint holds.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cua.agent.browser import ActionError, BrowserSession
from cua.artifact.schema import (
    Artifact,
    Condition,
    LocatorResolutionError,
    LocatorSpec,
    OutputField,
    Step,
    condition_holds,
    resolve_locator_chain,
)
from cua.artifact.store import match_event
from cua.safety.allowlist import AllowlistPolicy, AllowlistViolation

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class ReplayInputError(ValueError):
    """Inputs given to replay() don't satisfy the artifact's declared
    contract -- missing a required input, or a value of the wrong type.
    Raised before the browser ever starts."""


@dataclass
class ReplayResult:
    status: str  # "success" | "business_outcome" | "failure"
    outputs: dict[str, Any] | None = None
    outcome_name: str | None = None
    outcome_source: str | None = None  # "artifact" | "event_library"
    summary: str = ""
    failed_step: str | None = None
    expected: str | None = None
    observed: str | None = None
    drift: list[dict] = field(default_factory=list)  # [{step_id, tier_used, chain_len}]


# -- placeholder substitution, applied at replay time, uniformly --------------


def _substitute(value: str, inputs: dict[str, Any]) -> str:
    def _repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name not in inputs:
            return m.group(0)
        return str(inputs[name])

    return _PLACEHOLDER_RE.sub(_repl, value)


def _resolved_chain(chain: tuple[LocatorSpec, ...], inputs: dict[str, Any]) -> tuple[LocatorSpec, ...]:
    return tuple(LocatorSpec(by=l.by, value=_substitute(l.value, inputs), tag=l.tag, nth=l.nth) for l in chain)


def _resolved_condition(condition: Condition, inputs: dict[str, Any]) -> Condition:
    return Condition(kind=condition.kind, value=_substitute(condition.value, inputs))


# -- input validation -----------------------------------------------------


def _validate_inputs(artifact: Artifact, inputs: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    missing = []
    for param in artifact.inputs:
        if param.name in inputs:
            resolved[param.name] = _coerce(param.name, param.type, inputs[param.name])
        elif param.required:
            missing.append(param.name)
    if missing:
        raise ReplayInputError(f"missing required input(s): {sorted(missing)}")
    return resolved


def _coerce(name: str, type_: str, value: Any) -> Any:
    try:
        if type_ == "number":
            return float(value) if not isinstance(value, (int, float)) else value
        if type_ == "boolean":
            return bool(value) if not isinstance(value, bool) else value
        return str(value)
    except (TypeError, ValueError) as exc:
        raise ReplayInputError(f"input '{name}' could not be coerced to {type_}: {value!r}") from exc


# -- page-state helpers ----------------------------------------------------


def _page_text(browser: BrowserSession) -> str:
    return browser.page.evaluate("document.body.innerText") or ""


def _element_descriptors(browser: BrowserSession) -> list[str]:
    obs = browser.observe()
    return [f"{e.role}:{e.text}" for e in obs.elements]


def _check_outcomes(artifact: Artifact, browser: BrowserSession, inputs: dict[str, Any]) -> dict | None:
    page_text = _page_text(browser)
    url = browser.page.url
    descriptors = _element_descriptors(browser)

    for rule in artifact.outcome_table:
        signature = _resolved_condition(rule.signature, inputs)
        if condition_holds(signature, page_text, url, descriptors):
            return {"outcome_name": rule.outcome_name, "outcome_class": rule.outcome_class, "source": "artifact"}

    shared = match_event(page_text, url, descriptors)
    if shared:
        return {
            "outcome_name": shared["outcome_name"],
            "outcome_class": shared["outcome_class"],
            "source": "event_library",
        }
    return None


def _extract_outputs(artifact: Artifact, browser: BrowserSession) -> dict[str, Any]:
    page_text = _page_text(browser)
    outputs: dict[str, Any] = {}
    for field_spec in artifact.outputs:
        outputs[field_spec.name] = _extract_one(field_spec, page_text)
    return outputs


def _extract_one(field_spec: OutputField, page_text: str) -> Any:
    if field_spec.extract is None:
        return None
    raw: str | None = None
    if field_spec.extract.kind == "label_value":
        label = field_spec.extract.pattern
        for line in page_text.splitlines():
            if label in line:
                after = line.split(label, 1)[1].lstrip(" \t:\u00a0")
                raw = after.split("\t")[0].strip() if after else None
                if raw:
                    break
    elif field_spec.extract.kind == "regex":
        m = re.search(field_spec.extract.pattern, page_text)
        if m:
            raw = m.group(1)
    if raw is None:
        return None
    if field_spec.type == "number":
        cleaned = raw.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return raw
    if field_spec.type == "boolean":
        return raw.strip().lower() in ("true", "yes", "1")
    return raw


# -- the executor itself ----------------------------------------------------


def replay(
    artifact: Artifact,
    inputs: dict[str, Any],
    username: str,
    password: str,
    headless: bool = True,
    output_dir: str | None = None,
) -> ReplayResult:
    resolved_inputs = _validate_inputs(artifact, inputs)

    policy = AllowlistPolicy(allowed_origin=artifact.base_url_pattern)
    browser = BrowserSession(policy=policy, headless=headless)
    replay_trace: dict[str, Any] = {
        "artifact_id": artifact.artifact_id,
        "artifact_version": artifact.version,
        "inputs": resolved_inputs,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
    }
    trace_path = os.path.join(output_dir, "replay_trace.json") if output_dir else None

    def _save_trace() -> None:
        if trace_path:
            tmp = f"{trace_path}.tmp"
            with open(tmp, "w") as f:
                json.dump(replay_trace, f, indent=2, default=str)
            os.replace(tmp, trace_path)

    def _screenshot(step_num: int) -> str | None:
        if not output_dir:
            return None
        shots_dir = os.path.join(output_dir, "screenshots")
        os.makedirs(shots_dir, exist_ok=True)
        rel = os.path.join("screenshots", f"step_{step_num:03d}.png")
        try:
            browser.screenshot(os.path.join(output_dir, rel))
        except Exception:  # noqa: BLE001 -- evidence capture must never crash the replay
            return None
        return rel

    browser.start()
    drift: list[dict] = []
    try:
        browser.login(artifact.base_url_pattern, username, password)

        for i, step in enumerate(artifact.steps, start=1):
            # 1. Known outcome first -- a business/recoverable/hard_failure
            #    match always short-circuits, even before trying to act.
            outcome = _check_outcomes(artifact, browser, resolved_inputs)
            if outcome and outcome["outcome_class"] != "recoverable":
                return _finish_outcome(replay_trace, trace_path, _save_trace, outcome, drift)
            if outcome and outcome["outcome_class"] == "recoverable":
                _attempt_recovery(browser, artifact, username, password)

            chain = _resolved_chain(step.locator_chain, resolved_inputs)
            try:
                locator, tier = resolve_locator_chain(browser.page, chain)
            except LocatorResolutionError as exc:
                outcome = _check_outcomes(artifact, browser, resolved_inputs)
                if outcome:
                    return _finish_outcome(replay_trace, trace_path, _save_trace, outcome, drift)
                return _finish_failure(
                    replay_trace, trace_path, _save_trace, step,
                    expected=f"one of: {[l.describe() for l in chain]}", observed=str(exc), drift=drift,
                )

            if tier > 0:
                drift.append({"step_id": step.step_id, "tier_used": tier, "chain_len": len(chain)})

            try:
                _perform_action(browser, locator, step, resolved_inputs)
            except ActionError as exc:
                outcome = _check_outcomes(artifact, browser, resolved_inputs)
                if outcome:
                    return _finish_outcome(replay_trace, trace_path, _save_trace, outcome, drift)
                return _finish_failure(
                    replay_trace, trace_path, _save_trace, step,
                    expected=f"{step.action_type} to succeed", observed=str(exc), drift=drift,
                )
            except AllowlistViolation as exc:
                return _finish_failure(
                    replay_trace, trace_path, _save_trace, step,
                    expected="action within allowlist policy", observed=str(exc), drift=drift,
                )

            replay_trace["steps"].append(
                {"step_id": step.step_id, "action_type": step.action_type, "tier_used": tier, "screenshot": _screenshot(i)}
            )
            _save_trace()

            outcome = _check_outcomes(artifact, browser, resolved_inputs)
            if outcome and outcome["outcome_class"] != "recoverable":
                return _finish_outcome(replay_trace, trace_path, _save_trace, outcome, drift)

            if step.post_condition:
                resolved_cond = _resolved_condition(step.post_condition, resolved_inputs)
                if not condition_holds(resolved_cond, _page_text(browser), browser.page.url, _element_descriptors(browser)):
                    return _finish_failure(
                        replay_trace, trace_path, _save_trace, step,
                        expected=resolved_cond.describe(),
                        observed=f"url={browser.page.url!r}",
                        drift=drift,
                    )

        resolved_checkpoint = _resolved_condition(artifact.checkpoint, resolved_inputs)
        if not condition_holds(resolved_checkpoint, _page_text(browser), browser.page.url, _element_descriptors(browser)):
            outcome = _check_outcomes(artifact, browser, resolved_inputs)
            if outcome:
                return _finish_outcome(replay_trace, trace_path, _save_trace, outcome, drift)
            return _finish_failure(
                replay_trace, trace_path, _save_trace, None,
                expected=resolved_checkpoint.describe(), observed=f"url={browser.page.url!r}", drift=drift,
            )

        outputs = _extract_outputs(artifact, browser)
        replay_trace["result"] = {"status": "success", "outputs": outputs}
        _save_trace()
        return ReplayResult(
            status="success", outputs=outputs, summary="Checkpoint verified; outputs extracted.", drift=drift
        )
    finally:
        browser.close()


def _perform_action(browser: BrowserSession, locator, step: Step, inputs: dict[str, Any]) -> None:
    browser.policy.check_action_type(step.action_type)  # same public gate discovery's act_* use
    if step.action_type == "click":
        locator.click(timeout=5000)
        browser.page.wait_for_load_state("load", timeout=10000)
    elif step.action_type == "type_text":
        text = inputs[step.input_binding] if step.input_binding else (step.text_literal or "")
        locator.fill(str(text), timeout=5000)
    elif step.action_type == "select_option":
        value = inputs[step.input_binding] if step.input_binding else (step.select_value or "")
        locator.select_option(str(value), timeout=5000)
    elif step.action_type == "wait_for":
        browser.page.wait_for_load_state("load", timeout=10000)
    else:
        raise ActionError(f"unknown step action_type: {step.action_type}")
    browser.policy.check_url(browser.page.url)


def _attempt_recovery(browser: BrowserSession, artifact: Artifact, username: str, password: str) -> None:
    """Bounded, specific recovery -- not open-ended retry logic. v1 handles
    exactly one real recoverable case beyond what BrowserSession already
    does for free (dialogs are auto-accepted at the session level): a
    session-timeout page, recovered by logging back in once. Anything else
    classified "recoverable" falls through unchanged and gets caught by the
    next outcome/post_condition/checkpoint check, which is an honest
    "detected but not auto-recovered" outcome rather than invented behavior
    -- see REPORT.md §7."""
    text = _page_text(browser)
    if "session" in text.lower() and "expired" in text.lower():
        browser.login(artifact.base_url_pattern, username, password)


def _finish_outcome(trace, trace_path, save, outcome, drift) -> ReplayResult:
    trace["result"] = {"status": "business_outcome", **outcome}
    save()
    return ReplayResult(
        status="business_outcome",
        outcome_name=outcome["outcome_name"],
        outcome_source=outcome["source"],
        summary=f"Matched known outcome '{outcome['outcome_name']}' ({outcome['outcome_class']}, via {outcome['source']}).",
        drift=drift,
    )


def _finish_failure(trace, trace_path, save, step, *, expected, observed, drift) -> ReplayResult:
    trace["result"] = {
        "status": "failure",
        "failed_step": step.step_id if step else None,
        "expected": expected,
        "observed": observed,
    }
    save()
    return ReplayResult(
        status="failure",
        failed_step=step.step_id if step else None,
        expected=expected,
        observed=observed,
        summary=f"Replay failed at {step.step_id if step else 'final checkpoint'}: expected {expected}, observed {observed}.",
        drift=drift,
    )
