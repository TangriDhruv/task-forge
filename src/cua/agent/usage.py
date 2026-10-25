"""Token usage + cost logging for every Claude API call this project makes
(discovery decisions in ``cua.agent.llm``, routing in ``cua.agent.router``).

Every call is appended to one cumulative ledger (``data/usage.jsonl``)
**synchronously, immediately after the response comes back** -- the API
charge already happened by that point regardless of what the run does
next, so usage is recorded even if the run is later killed, crashes, or
hits ``max_steps``. This is the file to point any cost-analysis tooling
at; ``summarize()`` below is a minimal reducer over it, and
``scripts/usage_report.py`` is a thin CLI over that.

Pricing is a lookup table, not a live fetch -- appropriate simplicity for
a personal project's own cost tracking, not a billing-grade integration.
**USD per 1M tokens, as of 2026-10-06 (Anthropic's published rates).**
Update ``PRICING_PER_MTOK``/``WEB_SEARCH_COST_PER_USE`` if pricing
changes; nothing else in this module needs editing. An unrecognized model
contributes 0 estimated cost rather than guessing.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# src/cua/agent/usage.py -> parents[3] is the repo root (matches
# cua.artifact.store.DEFAULT_DB_PATH's same parents[3] pattern).
DEFAULT_USAGE_LOG = Path(__file__).resolve().parents[3] / "data" / "usage.jsonl"

PRICING_PER_MTOK = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}
# Cache tokens price off the same model's input rate via a multiplier
# (Anthropic prices cache writes/reads this way for every model above),
# not a separate per-model table.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1
# USD per web_search tool use (server-side tool, billed independently of
# tokens).
WEB_SEARCH_COST_PER_USE = 0.01


@dataclass
class UsageRecord:
    at: str
    call_site: str  # "discovery" | "routing"
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    web_search_requests: int = 0
    estimated_cost_usd: float = 0.0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    web_search_requests: int,
) -> float:
    rates = PRICING_PER_MTOK.get(model)
    if rates is None:
        return 0.0
    cost = (input_tokens / 1_000_000) * rates["input"]
    cost += (output_tokens / 1_000_000) * rates["output"]
    cost += (cache_creation_input_tokens / 1_000_000) * rates["input"] * CACHE_WRITE_MULTIPLIER
    cost += (cache_read_input_tokens / 1_000_000) * rates["input"] * CACHE_READ_MULTIPLIER
    cost += web_search_requests * WEB_SEARCH_COST_PER_USE
    return cost


def record_usage(response, call_site: str, log_path: Path | str | None = None) -> UsageRecord:
    """Call once per Claude API response (after ``client.messages.create``
    returns). Extracts ``response.usage``, estimates cost, and appends to
    the cumulative ledger immediately.

    ``log_path`` resolves ``DEFAULT_USAGE_LOG`` at call time, not as a bound
    default argument -- so tests can monkeypatch the module-level constant
    and have it actually take effect, rather than silently writing into the
    real ledger every time a fake response drives this function.

    NOTE: ``web_search_requests`` is read defensively via ``getattr`` with
    a 0 fallback at every level -- the exact shape of
    ``usage.server_tool_use`` hasn't been spot-checked against a real
    response in this codebase yet (verify next time a live run uses
    web_search; see CLAUDE.md)."""
    if log_path is None:
        log_path = DEFAULT_USAGE_LOG
    usage = response.usage
    server_tool_use = getattr(usage, "server_tool_use", None)
    web_search_requests = getattr(server_tool_use, "web_search_requests", 0) or 0

    record = UsageRecord(
        at=_now(),
        call_site=call_site,
        model=getattr(response, "model", "unknown"),
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        web_search_requests=web_search_requests,
    )
    record.estimated_cost_usd = _estimate_cost(
        record.model,
        record.input_tokens,
        record.output_tokens,
        record.cache_creation_input_tokens,
        record.cache_read_input_tokens,
        record.web_search_requests,
    )

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(asdict(record)) + "\n")
    return record


def summarize(log_path: Path | str = DEFAULT_USAGE_LOG) -> dict:
    """Aggregate the cumulative ledger: overall totals, broken down by
    model and by call_site. Used by scripts/usage_report.py."""
    log_path = Path(log_path)
    if not log_path.exists():
        return {
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_creation_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_web_search_requests": 0,
            "total_cost_usd": 0.0,
            "by_model": {},
            "by_call_site": {},
        }

    totals = {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "searches": 0, "cost": 0.0}
    by_model: dict[str, dict] = {}
    by_call_site: dict[str, dict] = {}

    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            totals["calls"] += 1
            totals["input"] += r["input_tokens"]
            totals["output"] += r["output_tokens"]
            totals["cache_write"] += r.get("cache_creation_input_tokens", 0)
            totals["cache_read"] += r.get("cache_read_input_tokens", 0)
            totals["searches"] += r.get("web_search_requests", 0)
            totals["cost"] += r["estimated_cost_usd"]

            m = by_model.setdefault(
                r["model"], {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
            )
            m["calls"] += 1
            m["cost_usd"] += r["estimated_cost_usd"]
            m["input_tokens"] += r["input_tokens"]
            m["output_tokens"] += r["output_tokens"]

            c = by_call_site.setdefault(r["call_site"], {"calls": 0, "cost_usd": 0.0})
            c["calls"] += 1
            c["cost_usd"] += r["estimated_cost_usd"]

    for m in by_model.values():
        m["cost_usd"] = round(m["cost_usd"], 4)
    for c in by_call_site.values():
        c["cost_usd"] = round(c["cost_usd"], 4)

    return {
        "total_calls": totals["calls"],
        "total_input_tokens": totals["input"],
        "total_output_tokens": totals["output"],
        "total_cache_creation_tokens": totals["cache_write"],
        "total_cache_read_tokens": totals["cache_read"],
        "total_web_search_requests": totals["searches"],
        "total_cost_usd": round(totals["cost"], 4),
        "by_model": by_model,
        "by_call_site": by_call_site,
    }
