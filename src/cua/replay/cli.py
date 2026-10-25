"""CLI entrypoint: `python -m cua.replay --artifact-id ... --input k=v ...`

The production invocation path is a direct call to `replay()` (an AI agent
product calling this as a library, per assignment.md's through-line) --
this CLI exists so the brief's required demo path ("the literal command(s)
to run the agent on a goal, then replay the resulting artifact") is one
real, runnable command, not a snippet to copy into a REPL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from cua.artifact.store import load_artifact

from .executor import ReplayInputError, replay


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--input must be key=value, got: {pair!r}")
        k, v = pair.split("=", 1)
        out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Deterministically replay a saved artifact -- no LLM involved.")
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--version", type=int, default=None, help="Pin a specific artifact version; default: latest.")
    parser.add_argument("--input", action="append", default=[], metavar="KEY=VALUE", help="Repeatable.")
    parser.add_argument("--target", required=True, help="Base URL of the live target app, e.g. http://127.0.0.1:5055")
    parser.add_argument("--username", default=os.environ.get("TARGET_APP_USERNAME", "demo"))
    parser.add_argument("--password", default=os.environ.get("TARGET_APP_PASSWORD", "demo"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--output-dir", default=None, help="Default: evidence/replay-<timestamp>/")
    args = parser.parse_args(argv)

    artifact = load_artifact(args.artifact_id, version=args.version)
    if artifact is None:
        print(f"No saved artifact found for id={args.artifact_id!r}.", file=sys.stderr)
        return 2

    output_dir = args.output_dir or os.path.join(
        "evidence", f"replay-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    os.makedirs(output_dir, exist_ok=True)

    try:
        result = replay(
            artifact,
            inputs=_parse_kv(args.input),
            username=args.username,
            password=args.password,
            headless=not args.headed,
            output_dir=output_dir,
        )
    except ReplayInputError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2

    print(f"Status: {result.status}")
    print(f"Summary: {result.summary}")
    if result.outputs:
        print(f"Outputs: {json.dumps(result.outputs, indent=2)}")
    if result.outcome_name:
        print(f"Outcome: {result.outcome_name} (source: {result.outcome_source})")
    if result.failed_step:
        print(f"Failed step: {result.failed_step}\nExpected: {result.expected}\nObserved: {result.observed}")
    if result.drift:
        print(f"Drift (fallback tiers used): {result.drift}")
    print(f"Replay trace saved to: {os.path.join(output_dir, 'replay_trace.json')}")

    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
