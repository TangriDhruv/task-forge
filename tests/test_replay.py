"""Tests for cua.replay.executor.

The control-flow parts of replay() (walking steps, resolving locators
against a live page, checking outcomes) are validated against the real
target app as part of generating /evidence/ -- faking a Playwright Page
convincingly enough to be meaningful isn't worth it here. What's tested
directly: the import-graph guarantee that makes "no LLM in replay" a fact
about the code rather than a comment, and every pure function (placeholder
substitution, type coercion, output extraction) that doesn't need a browser
to exercise.
"""

from __future__ import annotations

import ast
import os

import pytest

from cua.artifact.schema import Extraction, OutputField
from cua.replay.executor import ReplayInputError, _coerce, _extract_one, _resolved_chain, _resolved_condition, _substitute
from cua.artifact.schema import Condition, LocatorSpec


def _imported_modules(path: str) -> set[str]:
    tree = ast.parse(open(path).read(), filename=path)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_replay_package_never_imports_the_llm_or_discovery_loop():
    replay_dir = os.path.join(os.path.dirname(__file__), "..", "src", "cua", "replay")
    for root, _dirs, files in os.walk(replay_dir):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            mods = _imported_modules(path)
            forbidden = [m for m in mods if "cua.agent.llm" in m or "cua.agent.loop" in m or m == "anthropic"]
            assert not forbidden, f"{path} imports forbidden module(s): {forbidden}"


# -- substitution -------------------------------------------------------------


def test_substitute_replaces_known_placeholder():
    assert _substitute("/member/{member_id}/transfer", {"member_id": "M-10002"}) == "/member/M-10002/transfer"


def test_substitute_leaves_unknown_placeholder_untouched():
    assert _substitute("/member/{mystery}", {"member_id": "M-10002"}) == "/member/{mystery}"


def test_resolved_chain_substitutes_every_locator_in_the_chain():
    chain = (
        LocatorSpec(by="href", value="/member/{member_id}"),
        LocatorSpec(by="role_text", value="{member_id}", tag="a"),
    )
    resolved = _resolved_chain(chain, {"member_id": "M-10003"})
    assert resolved[0].value == "/member/M-10003"
    assert resolved[1].value == "M-10003"


def test_resolved_condition_substitutes():
    cond = Condition(kind="text_contains", value="Member {member_id}")
    resolved = _resolved_condition(cond, {"member_id": "M-10004"})
    assert resolved.value == "Member M-10004"


# -- coercion -----------------------------------------------------------------


def test_coerce_number():
    assert _coerce("amount", "number", "50") == 50.0


def test_coerce_boolean():
    assert _coerce("confirmed", "boolean", True) is True


def test_coerce_bad_number_raises_replay_input_error():
    with pytest.raises(ReplayInputError):
        _coerce("amount", "number", "not-a-number")


# -- output extraction ---------------------------------------------------------


def test_extract_label_value_from_tab_separated_line():
    field_spec = OutputField(name="transfer_id", type="string", extract=Extraction(kind="label_value", pattern="Transfer ID"))
    page_text = "TRANSFER COMPLETED.\n\nTransfer ID\tXFR-233353\nFrom Account\tCHK-10001-01\nAmount\t$50.00"
    assert _extract_one(field_spec, page_text) == "XFR-233353"


def test_extract_label_value_as_number_strips_currency_formatting():
    field_spec = OutputField(name="amount", type="number", extract=Extraction(kind="label_value", pattern="Amount"))
    page_text = "Transfer ID\tXFR-1\nAmount\t$50.00\nStatus\tCompleted"
    assert _extract_one(field_spec, page_text) == 50.0


def test_extract_regex_capture_group():
    field_spec = OutputField(name="ref", type="string", extract=Extraction(kind="regex", pattern=r"Reference ID: (\S+)"))
    page_text = "TRANSFER COMPLETED. Reference ID: XFR-233353"
    assert _extract_one(field_spec, page_text) == "XFR-233353"


def test_extract_missing_label_returns_none():
    field_spec = OutputField(name="missing", type="string", extract=Extraction(kind="label_value", pattern="Nonexistent Label"))
    assert _extract_one(field_spec, "some unrelated page text") is None


def test_extract_with_no_extraction_rule_returns_none():
    field_spec = OutputField(name="no_rule", type="string")
    assert _extract_one(field_spec, "Transfer ID\tXFR-1") is None
