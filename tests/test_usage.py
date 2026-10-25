"""Tests for cua.agent.usage. Uses fake response objects throughout --
credits were exhausted mid-session, so these can't be checked against a
real response right now; the ``server_tool_use`` field shape in
``record_usage`` is read defensively (getattr with 0 fallback) for exactly
this reason and should be spot-checked against a real web_search response
once credits are available again (see CLAUDE.md)."""

from __future__ import annotations

from types import SimpleNamespace

from cua.agent.usage import record_usage, summarize


def _fake_response(model="claude-sonnet-5", input_tokens=1000, output_tokens=200, cache_creation=0, cache_read=0, web_search_requests=0):
    server_tool_use = SimpleNamespace(web_search_requests=web_search_requests) if web_search_requests else None
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        server_tool_use=server_tool_use,
    )
    return SimpleNamespace(model=model, usage=usage)


def test_record_usage_computes_cost_for_known_model(tmp_path):
    log_path = tmp_path / "usage.jsonl"
    response = _fake_response(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000)
    record = record_usage(response, call_site="discovery", log_path=log_path)
    # sonnet-5: $2/$10 per MTok -> 1M in + 1M out = $12.00
    assert record.estimated_cost_usd == 12.0
    assert log_path.exists()


def test_record_usage_appends_to_ledger(tmp_path):
    log_path = tmp_path / "usage.jsonl"
    record_usage(_fake_response(), call_site="discovery", log_path=log_path)
    record_usage(_fake_response(), call_site="routing", log_path=log_path)
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_unknown_model_contributes_zero_cost_not_a_guess(tmp_path):
    log_path = tmp_path / "usage.jsonl"
    response = _fake_response(model="some-future-model-not-in-table", input_tokens=500_000, output_tokens=500_000)
    record = record_usage(response, call_site="discovery", log_path=log_path)
    assert record.estimated_cost_usd == 0.0


def test_cache_tokens_priced_off_input_rate_with_multipliers(tmp_path):
    log_path = tmp_path / "usage.jsonl"
    # sonnet-5 input rate $2/MTok. 1M cache-write tokens -> 2 * 1.25 = $2.50.
    # 1M cache-read tokens -> 2 * 0.1 = $0.20. Plus 0 base input/output.
    response = _fake_response(model="claude-sonnet-5", input_tokens=0, output_tokens=0, cache_creation=1_000_000, cache_read=1_000_000)
    record = record_usage(response, call_site="discovery", log_path=log_path)
    assert round(record.estimated_cost_usd, 4) == 2.70


def test_web_search_requests_recorded_and_priced(tmp_path):
    log_path = tmp_path / "usage.jsonl"
    response = _fake_response(model="claude-sonnet-5", input_tokens=0, output_tokens=0, web_search_requests=3)
    record = record_usage(response, call_site="routing", log_path=log_path)
    assert record.web_search_requests == 3
    assert round(record.estimated_cost_usd, 4) == 0.03  # 3 * $0.01


def test_missing_server_tool_use_defaults_to_zero_searches(tmp_path):
    """Defensive getattr path -- server_tool_use is None (or absent, or
    lacking the attribute) on a plain non-search response."""
    log_path = tmp_path / "usage.jsonl"
    response = _fake_response(web_search_requests=0)
    record = record_usage(response, call_site="discovery", log_path=log_path)
    assert record.web_search_requests == 0


def test_summarize_empty_ledger(tmp_path):
    summary = summarize(tmp_path / "does_not_exist.jsonl")
    assert summary["total_calls"] == 0
    assert summary["total_cost_usd"] == 0.0


def test_summarize_aggregates_across_models_and_call_sites(tmp_path):
    log_path = tmp_path / "usage.jsonl"
    record_usage(_fake_response(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0), call_site="discovery", log_path=log_path)
    record_usage(_fake_response(model="claude-opus-5", input_tokens=1_000_000, output_tokens=0), call_site="routing", log_path=log_path)

    summary = summarize(log_path)
    assert summary["total_calls"] == 2
    assert summary["by_model"]["claude-sonnet-5"]["calls"] == 1
    assert summary["by_model"]["claude-opus-5"]["calls"] == 1
    assert summary["by_call_site"]["discovery"]["calls"] == 1
    assert summary["by_call_site"]["routing"]["calls"] == 1
    # sonnet-5 1M input = $2.00, opus-5 1M input = $5.00
    assert round(summary["total_cost_usd"], 4) == 7.00
