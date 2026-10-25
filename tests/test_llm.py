"""Regression test for a real bug hit during a live discovery run: the model
occasionally emits more than one `tool_use` block in a single turn (observed
with extended thinking active) despite `tool_choice="any"` only forcing *at
least* one. The loop executes exactly one action per turn, so the second,
unexecuted `tool_use` block was being left in message history with no
matching `tool_result` -- which the Anthropic API rejects on the *next*
call. `Decider._call_model` must truncate to the first `tool_use` block
before recording the turn."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import cua.agent.llm as llm


@dataclass
class _FakeBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None

    def model_dump(self):
        # Real SDK blocks (e.g. "thinking") have this; _serialize_content
        # falls back to it for any type it doesn't special-case.
        return {"type": self.type, "text": self.text}


class _FakeMessages:
    def __init__(self, content_blocks):
        self._content_blocks = content_blocks

    def create(self, **kwargs):
        return SimpleNamespace(content=self._content_blocks)


def _decider_with_fake_response(content_blocks) -> llm.Decider:
    decider = llm.Decider(goal="test goal", model="fake-model")
    decider._client = SimpleNamespace(messages=_FakeMessages(content_blocks))
    return decider


def test_single_tool_use_is_unaffected():
    content = [
        _FakeBlock(type="text", text="I'll click the link."),
        _FakeBlock(type="tool_use", id="toolu_1", name="click", input={"ref": 5}),
    ]
    decider = _decider_with_fake_response(content)
    decision = decider.start({"url": "x"})

    assert decision.tool_name == "click"
    assert decision.tool_use_id == "toolu_1"
    recorded = decider.messages[-1]["content"]
    assert [b["type"] for b in recorded] == ["text", "tool_use"]


def test_extra_dangling_tool_use_blocks_are_dropped():
    content = [
        _FakeBlock(type="thinking", text=None),
        _FakeBlock(type="tool_use", id="toolu_first", name="click", input={"ref": 5}),
        _FakeBlock(type="tool_use", id="toolu_second_dangling", name="click", input={"ref": 4}),
    ]
    decider = _decider_with_fake_response(content)
    decision = decider.start({"url": "x"})

    # The Decision must reference the FIRST tool_use -- that's what the loop
    # will actually execute and send a tool_result for.
    assert decision.tool_use_id == "toolu_first"

    # The recorded assistant turn must not contain the second tool_use block
    # at all -- if it did, the *next* API call would fail with "tool_use ids
    # were found without tool_result blocks" since nothing ever answers it.
    recorded = decider.messages[-1]["content"]
    tool_use_ids = [b["id"] for b in recorded if b["type"] == "tool_use"]
    assert tool_use_ids == ["toolu_first"]


def test_respond_only_needs_to_answer_the_kept_tool_use():
    content = [
        _FakeBlock(type="tool_use", id="toolu_first", name="click", input={"ref": 5}),
        _FakeBlock(type="tool_use", id="toolu_dangling", name="click", input={"ref": 9}),
    ]
    decider = _decider_with_fake_response(content)
    decision = decider.start({"url": "x"})

    # Simulate the next turn's response (a normal single tool_use) to prove
    # the message history built from the first turn is self-consistent --
    # this is exactly the shape the real API validates.
    next_content = [_FakeBlock(type="tool_use", id="toolu_next", name="finish_success", input={"summary": "done", "outputs": {}})]
    decider._client = SimpleNamespace(messages=_FakeMessages(next_content))
    next_decision = decider.respond(decision.tool_use_id, {"ok": True})

    assert next_decision.tool_name == "finish_success"
    # messages: [0]=start's user msg, [1]=first assistant turn (kept),
    # [2]=the tool_result for toolu_first. It must not also need to answer
    # toolu_dangling, since that block was never recorded in [1].
    tool_result_ids = [c["tool_use_id"] for c in decider.messages[2]["content"]]
    assert tool_result_ids == ["toolu_first"]
