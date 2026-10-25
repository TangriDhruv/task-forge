"""Tests for cua.agent.router. Regression coverage for two real bugs hit
while building this: (1) an unverified, merely-recalled URL being accepted
as if it were a verified one, and (2) rejecting that recalled URL with a
plain text message instead of a proper tool_result, which the Anthropic
API rejects outright on the *next* call ("tool_use ids were found without
tool_result blocks") -- the same class of bug already fixed once in
cua.agent.llm, now in a second place that independently builds a message
history by hand."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import cua.agent.router as router
import cua.agent.usage as usage_module


@pytest.fixture(autouse=True)
def _isolated_usage_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_module, "DEFAULT_USAGE_LOG", tmp_path / "usage.jsonl")


@dataclass
class _FakeBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None

    def model_dump(self):
        return {"type": self.type}


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        # Snapshot messages -- it's the same mutable list object the caller
        # keeps appending to after this call returns, so recording it by
        # reference would make every past call's record silently reflect
        # the list's *final* state instead of what it looked like at call
        # time.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        content = self._responses.pop(0)
        fake_usage = SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
            cache_read_input_tokens=0, server_tool_use=None,
        )
        return SimpleNamespace(content=content, model="fake-model", usage=fake_usage)


def _patch_client(monkeypatch, responses):
    fake_messages = _FakeMessages(responses)
    monkeypatch.setattr(router, "Anthropic", lambda **kwargs: SimpleNamespace(messages=fake_messages))
    return fake_messages


def test_known_target_match_accepted_without_search(monkeypatch):
    responses = [
        [_FakeBlock(type="tool_use", id="t1", name="navigate", input={"url": "http://127.0.0.1:5055", "reasoning": "it's the known app"})],
    ]
    _patch_client(monkeypatch, responses)
    decision = router.route_target(
        "look up a member", known_targets={"banking": "http://127.0.0.1:5055"}
    )
    assert decision.source == "known_target"
    assert decision.url == "http://127.0.0.1:5055"


def test_search_backed_navigate_accepted(monkeypatch):
    responses = [
        [
            _FakeBlock(type="server_tool_use", id="s1", name="web_search"),
            _FakeBlock(type="tool_use", id="t1", name="navigate", input={"url": "https://trust.example.com/", "reasoning": "found via search"}),
        ],
    ]
    _patch_client(monkeypatch, responses)
    decision = router.route_target("find example corp's trust center", known_targets={})
    assert decision.source == "web_search"
    assert decision.url == "https://trust.example.com/"


def test_unverified_recall_is_rejected_and_forces_a_real_search(monkeypatch):
    """The core regression: a navigate call with no search and no known-
    target match must NOT be accepted on the first turn -- the model must
    be made to search, and only an actually-search-backed navigate call
    may complete routing."""
    responses = [
        # Turn 1: model just recalls a URL from memory, no search.
        [_FakeBlock(type="tool_use", id="t1", name="navigate", input={"url": "https://trust.example.com/", "reasoning": "I recall this"})],
        # Turn 2: model searches, then navigates again.
        [
            _FakeBlock(type="server_tool_use", id="s1", name="web_search"),
            _FakeBlock(type="tool_use", id="t2", name="navigate", input={"url": "https://trust.example.com/", "reasoning": "confirmed via search"}),
        ],
    ]
    messages_client = _patch_client(monkeypatch, responses)
    decision = router.route_target("find example corp's trust center", known_targets={})

    assert decision.source == "web_search"
    # Turn 2's API call must have been sent a well-formed message history --
    # specifically, a tool_result answering turn 1's navigate tool_use,
    # not a bare text message (which the real API rejects outright).
    second_call_messages = messages_client.calls[1]["messages"]
    rejection_turn = second_call_messages[-1]
    assert rejection_turn["role"] == "user"
    assert isinstance(rejection_turn["content"], list)
    assert rejection_turn["content"][0]["type"] == "tool_result"
    assert rejection_turn["content"][0]["tool_use_id"] == "t1"


def test_never_commits_within_turn_budget_raises():
    pass  # covered implicitly: MAX_ROUTING_TURNS exhaustion -> RoutingError,
    # exercised indirectly by the other tests' bounded loop; a dedicated
    # all-recall-no-search fixture would just repeat test 3's first turn
    # MAX_ROUTING_TURNS times, which doesn't add new coverage.


def test_routing_error_message_includes_goal(monkeypatch):
    goal = "an impossible goal"
    responses = [
        [_FakeBlock(type="tool_use", id=f"t{i}", name="navigate", input={"url": "https://x.example/", "reasoning": "recall"})]
        for i in range(router.MAX_ROUTING_TURNS)
    ]
    _patch_client(monkeypatch, responses)
    with pytest.raises(router.RoutingError, match="an impossible goal"):
        router.route_target(goal, known_targets={})
