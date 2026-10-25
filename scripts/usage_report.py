#!/usr/bin/env python3
"""Print a summary of token usage and estimated cost across every Claude
API call this project has made (discovery + routing), from the cumulative
ledger at data/usage.jsonl.

Usage: .venv/bin/python scripts/usage_report.py
"""

from __future__ import annotations

from cua.agent.usage import DEFAULT_USAGE_LOG, summarize


def main() -> None:
    summary = summarize()
    if summary["total_calls"] == 0:
        print(f"No usage recorded yet at {DEFAULT_USAGE_LOG}")
        return

    print(f"Usage ledger: {DEFAULT_USAGE_LOG}\n")
    print(f"Total API calls:        {summary['total_calls']}")
    print(f"Total input tokens:     {summary['total_input_tokens']:,}")
    print(f"Total output tokens:    {summary['total_output_tokens']:,}")
    print(f"Cache write tokens:     {summary['total_cache_creation_tokens']:,}")
    print(f"Cache read tokens:      {summary['total_cache_read_tokens']:,}")
    print(f"Web searches used:      {summary['total_web_search_requests']}")
    print(f"Estimated total cost:   ${summary['total_cost_usd']:.4f}")

    print("\nBy model:")
    for model, m in sorted(summary["by_model"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        print(f"  {model:<22} {m['calls']:>4} calls   ${m['cost_usd']:.4f}   (in={m['input_tokens']:,} out={m['output_tokens']:,})")

    print("\nBy call site:")
    for site, c in sorted(summary["by_call_site"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        print(f"  {site:<22} {c['calls']:>4} calls   ${c['cost_usd']:.4f}")


if __name__ == "__main__":
    main()
