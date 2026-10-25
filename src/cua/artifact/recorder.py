"""Turn a successful discovery ``RunTrace`` into a saved, replayable
``Artifact`` (assignment.md §3.2) -- the seam between "the model discovered
how to do this once" and "the artifact is a reusable capability."

Deliberately mechanical and explicit, not inferred: the caller (a human
reviewing the trace, or an orchestrating script that already knows what
concrete values it asked the agent to use) supplies ``bindings`` -- a map of
``param_name -> the concrete value that appeared in this specific run``
(e.g. ``{"member_id": "M-10001", "amount": "50"}``). The recorder's only job
is to find every occurrence of those concrete values inside the trace's
locator values and typed/selected values and replace them with
``{param_name}`` placeholders, so the resulting artifact works for *any*
member/amount, not just the one that happened to be used during discovery.
Nothing here guesses at parameterization -- a wrong guess would be far
worse than requiring one explicit, reviewable list of bindings up front.

Each artifact-level ``checkpoint`` is likewise caller-supplied, not
auto-extracted from "whatever text looked distinctive" -- see REPORT.md §7
for why that's a documented simplification rather than a silent gap: a
per-step ``post_condition`` is supported by the schema but this recorder
does not populate one automatically, since deriving a *general* (not
member-specific) one from a single run's diff is exactly the kind of fuzzy
inference this module avoids. A human reviewing a recorded artifact can add
stricter per-step conditions by hand before approving it.
"""

from __future__ import annotations

from cua.agent.trace import RunTrace, StepRecord

from .schema import Artifact, Condition, InputParam, LocatorSpec, OutcomeRule, OutputField

# Steps that never correspond to a replayable browser action.
_NON_REPLAYABLE_TOOLS = {"finish_success", "finish_stuck", "request_human_help"}


class RecorderError(RuntimeError):
    """A trace can't be mechanically turned into an artifact as given --
    e.g. a step has no captured locator_chain (an old-format trace; see
    cua.agent.trace.StepRecord)."""


def _mask(value: str, bindings: dict[str, str]) -> str:
    """Replace every occurrence of a bound concrete value with its
    ``{param_name}`` placeholder. Longest values first, so one bound value
    that happens to be a substring of another (e.g. "10" inside "M-10001")
    can never partially clobber the longer, more specific match."""
    for param_name, concrete in sorted(bindings.items(), key=lambda kv: -len(kv[1])):
        if concrete:
            value = value.replace(concrete, "{" + param_name + "}")
    return value


def _mask_locator(raw: dict, bindings: dict[str, str]) -> LocatorSpec:
    return LocatorSpec(
        by=raw["by"], value=_mask(raw["value"], bindings), tag=raw.get("tag"), nth=raw.get("nth", 0)
    )


def _find_binding(value: str, bindings: dict[str, str]) -> str | None:
    """A typed/selected value that *exactly* equals one bound concrete value
    is that parameter, full stop -- not masked as a substring (a typed
    amount "50" is wholly the `amount` input, not a literal with "50"
    replaced inside some larger fixed string)."""
    for param_name, concrete in bindings.items():
        if value == concrete:
            return param_name
    return None


def _build_steps(trace: RunTrace, bindings: dict[str, str]) -> list:
    from .schema import Step

    steps: list[Step] = []
    for sr in trace.steps:
        if sr.tool_name in _NON_REPLAYABLE_TOOLS or sr.tool_name.startswith("human_"):
            continue
        if sr.locator_chain is None:
            raise RecorderError(
                f"step {sr.step} ({sr.tool_name}) has no captured locator_chain -- this trace "
                "predates StepRecord.locator_chain (see trace.py) and can't be mechanically "
                "recorded; re-run discovery to get a trace with a real chain per step."
            )
        chain = tuple(_mask_locator(l, bindings) for l in sr.locator_chain)
        step_id = f"step_{sr.step:03d}"

        input_binding = None
        text_literal = None
        select_value = None
        if sr.tool_name == "type_text":
            text = sr.tool_input.get("text", "")
            input_binding = _find_binding(text, bindings)
            text_literal = None if input_binding else text
        elif sr.tool_name == "select_option":
            value = sr.tool_input.get("value", "")
            input_binding = _find_binding(value, bindings)
            select_value = None if input_binding else value

        steps.append(
            Step(
                step_id=step_id,
                action_type=sr.tool_name,
                locator_chain=chain,
                post_condition=None,
                input_binding=input_binding,
                text_literal=text_literal,
                select_value=select_value,
            )
        )
    return steps


def record_artifact(
    trace: RunTrace,
    *,
    artifact_id: str,
    name: str,
    goal_description: str,
    app_id: str,
    base_url_pattern: str,
    inputs: list[InputParam],
    outputs: list[OutputField],
    bindings: dict[str, str],
    checkpoint: Condition,
    outcome_table: list[OutcomeRule] | None = None,
    version: int = 1,
    created_from_run: str | None = None,
) -> Artifact:
    if trace.status != "success":
        raise RecorderError(f"can only record an artifact from a successful trace, got status={trace.status!r}")

    missing = {p.name for p in inputs if p.required} - set(bindings)
    if missing:
        raise RecorderError(f"bindings missing a concrete value for required input(s): {sorted(missing)}")

    steps = _build_steps(trace, bindings)
    masked_checkpoint = Condition(kind=checkpoint.kind, value=_mask(checkpoint.value, bindings))
    masked_outcomes = [
        OutcomeRule(
            signature=Condition(kind=o.signature.kind, value=_mask(o.signature.value, bindings)),
            outcome_class=o.outcome_class,
            outcome_name=o.outcome_name,
        )
        for o in (outcome_table or [])
    ]

    return Artifact(
        artifact_id=artifact_id,
        name=name,
        version=version,
        goal_description=goal_description,
        app_id=app_id,
        base_url_pattern=base_url_pattern,
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        checkpoint=masked_checkpoint,
        outcome_table=masked_outcomes,
        created_from_run=created_from_run,
        reviewed=False,
    )
