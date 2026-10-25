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

from .schema import Artifact, Condition, InputParam, ItemLocator, LocatorSpec, OutcomeRule, OutputField

# Steps that never correspond to a replayable browser action.
_NON_REPLAYABLE_TOOLS = {"finish_success", "finish_stuck", "request_human_help"}

# A run of fewer consecutive, structurally-similar clicks is more likely to
# be two unrelated single clicks that happen to be adjacent than an actual
# repeated list -- see _detect_repeat_runs.
_MIN_REPEAT_RUN = 2
# A shared prefix+suffix shorter than this (e.g. just "/") is too generic to
# trust as "these are the same kind of item," not a real structural signal.
_MIN_SHARED_PATTERN_LEN = 4


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


def _href_tier(sr: StepRecord) -> dict | None:
    """The href-based locator tier for a click step, if it has one -- the
    signal a repeated download/list pattern is detected from, since a
    repeated item's identity usually lives in its link target, not its
    visible text (which legacy table rows often repeat verbatim)."""
    if sr.tool_name != "click" or not sr.locator_chain:
        return None
    for l in sr.locator_chain:
        if l["by"] == "href":
            return l
    return None


def _common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    shortest = min(strings, key=len)
    for i, ch in enumerate(shortest):
        if any(s[i] != ch for s in strings):
            return shortest[:i]
    return shortest


def _common_suffix(strings: list[str]) -> str:
    return _common_prefix([s[::-1] for s in strings])[::-1]


def _detect_repeat_runs(steps: list[StepRecord]) -> list[tuple[int, int, str, str]]:
    """Find runs of consecutive click steps whose hrefs share a real
    common shape (e.g. every document link under the same path, or every
    file ending in the same extension) -- the recorded fingerprint of
    "click each item in this list," which should replay as one
    ``repeat_for_each`` over however many items exist, not N steps locked
    to this run's exact count. Returns (start, end_exclusive, prefix,
    suffix) tuples into ``steps``; non-overlapping, in order.

    Sharing an href prefix is necessary but not sufficient: a drill-down
    navigation (open a member, then open their transfer page) can share
    just as long a prefix as true siblings in a list, and is exactly the
    wrong thing to collapse. The deciding signal is that every step in a
    real repeated-list pattern was decided while looking at the *same*
    page (``observation["url"]`` unchanged) -- true siblings never
    navigate between clicks (each click triggers a download, not a page
    change); a drill-down always does.
    """
    runs: list[tuple[int, int, str, str]] = []
    i, n = 0, len(steps)
    while i < n:
        if _href_tier(steps[i]) is None:
            i += 1
            continue
        page_url = steps[i].observation.get("url")
        j = i + 1
        while (
            j < n
            and _href_tier(steps[j]) is not None
            and steps[j].observation.get("url") == page_url
        ):
            j += 1
        if j - i >= _MIN_REPEAT_RUN:
            hrefs = [_href_tier(s)["value"] for s in steps[i:j]]
            prefix, suffix = _common_prefix(hrefs), _common_suffix(hrefs)
            if len(prefix) + len(suffix) >= _MIN_SHARED_PATTERN_LEN:
                runs.append((i, j, prefix, suffix))
                i = j
                continue
        i += 1
    return runs


def _build_steps(trace: RunTrace, bindings: dict[str, str]) -> list:
    from .schema import Step

    replayable = [
        sr for sr in trace.steps
        if sr.tool_name not in _NON_REPLAYABLE_TOOLS and not sr.tool_name.startswith("human_")
    ]
    for sr in replayable:
        if sr.locator_chain is None:
            raise RecorderError(
                f"step {sr.step} ({sr.tool_name}) has no captured locator_chain -- this trace "
                "predates StepRecord.locator_chain (see trace.py) and can't be mechanically "
                "recorded; re-run discovery to get a trace with a real chain per step."
            )

    runs_by_start = {start: (end, prefix, suffix) for start, end, prefix, suffix in _detect_repeat_runs(replayable)}

    steps: list[Step] = []
    idx = 0
    while idx < len(replayable):
        if idx in runs_by_start:
            end, prefix, suffix = runs_by_start[idx]
            css_value = "a"
            masked_prefix, masked_suffix = _mask(prefix, bindings), _mask(suffix, bindings)
            if masked_prefix:
                css_value += f'[href^="{masked_prefix}"]'
            if masked_suffix:
                css_value += f'[href$="{masked_suffix}"]'
            steps.append(
                Step(
                    step_id=f"step_{replayable[idx].step:03d}_repeat",
                    action_type="repeat_for_each",
                    locator_chain=(),
                    item_locator=ItemLocator(by="css", value=css_value),
                    # Defaults to True: the detected pattern is "N consecutive
                    # same-shaped link clicks," which in practice is almost
                    # always a download list -- a human reviewing this
                    # artifact should flip it if that's not actually the
                    # case here, per the schema's reviewed:bool gate.
                    expect_download=True,
                )
            )
            idx = end
            continue

        sr = replayable[idx]
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
        idx += 1
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
