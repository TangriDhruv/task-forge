"""Tests for cua.artifact.recorder: trace -> Artifact, with explicit
parameter bindings driving the masking."""

from __future__ import annotations

import pytest

from cua.agent.trace import RunTrace, StepRecord
from cua.artifact import Condition, InputParam, OutputField, RecorderError, record_artifact


def _fake_trace() -> RunTrace:
    trace = RunTrace(goal="transfer funds", base_url="http://127.0.0.1:5055")
    trace.status = "success"
    trace.steps = [
        StepRecord(
            step=1,
            observation={"url": "http://127.0.0.1:5055/search"},
            tool_name="click",
            tool_input={"ref": 5},
            locator_chain=[
                {"by": "href", "value": "/member/M-10001", "tag": None, "nth": 0},
                {"by": "role_text", "value": "M-10001", "tag": "a", "nth": 0},
                {"by": "css_nth", "value": "a", "tag": "a", "nth": 3},
            ],
        ),
        StepRecord(
            step=2,
            observation={"url": "http://127.0.0.1:5055/member/M-10001"},
            tool_name="click",
            tool_input={"ref": 4},
            locator_chain=[
                {"by": "href", "value": "/member/M-10001/transfer", "tag": None, "nth": 0},
                {"by": "role_text", "value": "Transfer Funds", "tag": "a", "nth": 0},
            ],
        ),
        StepRecord(
            step=3,
            observation={},
            tool_name="select_option",
            tool_input={"ref": 3, "value": "CHK-10001-01"},
            locator_chain=[{"by": "name", "value": "from_account", "tag": None, "nth": 0}],
        ),
        StepRecord(
            step=4,
            observation={},
            tool_name="type_text",
            tool_input={"ref": 5, "text": "50"},
            locator_chain=[{"by": "name", "value": "amount", "tag": None, "nth": 0}],
        ),
        StepRecord(
            step=5,
            observation={"url": "http://127.0.0.1:5055/member/M-10001/transfer"},
            tool_name="click",
            tool_input={"ref": 7},
            action_error=None,
            locator_chain=[{"by": "role_text", "value": "Submit Transfer", "tag": "button", "nth": 0}],
        ),
        StepRecord(
            step=6,
            observation={"visible_text": "TRANSFER COMPLETED. Reference ID: XFR-233353"},
            tool_name="finish_success",
            tool_input={"summary": "done", "outputs": {"transfer_id": "XFR-233353"}},
            locator_chain=None,
        ),
    ]
    return trace


BINDINGS = {"member_id": "M-10001", "from_account": "CHK-10001-01", "amount": "50"}


def _record(**overrides):
    kwargs = dict(
        trace=_fake_trace(),
        artifact_id="cu-transfer-funds",
        name="Transfer funds",
        goal_description="Transfer funds between a member's accounts.",
        app_id="cu-servicing-console",
        base_url_pattern="http://127.0.0.1:5055",
        inputs=[
            InputParam(name="member_id", type="string"),
            InputParam(name="from_account", type="string"),
            InputParam(name="amount", type="number"),
        ],
        outputs=[OutputField(name="transfer_id", type="string")],
        bindings=BINDINGS,
        checkpoint=Condition(kind="text_contains", value="TRANSFER COMPLETED"),
    )
    kwargs.update(overrides)
    return record_artifact(**kwargs)


def test_href_locator_is_masked_with_the_bound_param():
    artifact = _record()
    step1 = artifact.steps[0]
    assert step1.locator_chain[0].value == "/member/{member_id}"
    # The role_text tier's value is literally "M-10001" too (the link text
    # IS the member id) -- masking must apply there as well, so this
    # fallback tier would also generalize correctly if ever needed.
    assert step1.locator_chain[1].value == "{member_id}"


def test_second_step_href_masks_correctly_even_with_longer_path():
    artifact = _record()
    step2 = artifact.steps[1]
    assert step2.locator_chain[0].value == "/member/{member_id}/transfer"


def test_select_option_exact_match_becomes_input_binding_not_literal():
    artifact = _record()
    step3 = artifact.steps[2]
    assert step3.action_type == "select_option"
    assert step3.input_binding == "from_account"
    assert step3.select_value is None


def test_type_text_exact_match_becomes_input_binding_not_literal():
    artifact = _record()
    step4 = artifact.steps[3]
    assert step4.action_type == "type_text"
    assert step4.input_binding == "amount"
    assert step4.text_literal is None


def test_non_replayable_steps_are_skipped():
    artifact = _record()
    # 5 replayable steps (click, click, select_option, type_text, click) --
    # finish_success is not one of them.
    assert len(artifact.steps) == 5
    assert all(s.action_type != "finish_success" for s in artifact.steps)


def test_checkpoint_is_carried_through():
    artifact = _record()
    assert artifact.checkpoint.kind == "text_contains"
    assert artifact.checkpoint.value == "TRANSFER COMPLETED"


def test_missing_required_binding_raises():
    with pytest.raises(RecorderError):
        _record(bindings={"member_id": "M-10001"})  # missing from_account, amount


def test_step_without_locator_chain_raises_recorder_error():
    trace = _fake_trace()
    trace.steps[0].locator_chain = None
    with pytest.raises(RecorderError):
        _record(trace=trace)


def test_non_success_trace_raises():
    trace = _fake_trace()
    trace.status = "stuck"
    with pytest.raises(RecorderError):
        _record(trace=trace)
