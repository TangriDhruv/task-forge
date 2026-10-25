"""Scripted/non-interactive entrypoint: `python -m cua.agent --goal "..." --target http://...`

Kept specifically for scripted evidence capture (the `run-demo` skill) where
nobody is watching a dashboard to service a `request_human_help` pause --
see the `interactive=False` passed to `run_discovery` below, which makes any
such request time out almost immediately rather than hang. For interactive,
human-in-the-loop use, run `python -m cua.app` instead (CLAUDE.md:
"Architecture: single process, one web dashboard app, not a CLI")."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from cua.agent.loop import DEFAULT_MAX_STEPS, run_discovery, run_task


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the LLM-driven discovery agent on a goal.")
    parser.add_argument("--goal", required=True, help="Natural-language goal for the agent to accomplish.")
    parser.add_argument(
        "--target",
        default=None,
        help=(
            "Base URL of the target app, e.g. http://127.0.0.1:5055. Omit to auto-route the "
            "goal instead (cua.agent.router) -- known local targets still resolve directly; "
            "anything else is found via web search."
        ),
    )
    parser.add_argument(
        "--start-path",
        default="/search",
        help="Path to start the goal-specific flow from, after login (--target mode only). Default: /search",
    )
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument(
        "--no-login",
        action="store_true",
        help=(
            "Skip the login bootstrap step entirely -- for a real public site that needs no "
            "login (and almost certainly has no form matching our fixed username/password "
            "shape anyway). Without this, --target mode falls back to demo/demo, which is "
            "right for the mock apps but wrong for anything else."
        ),
    )
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Where to write trace.json and screenshots. Default: evidence/discovery-run-<timestamp>/",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (check your .env). Aborting.", file=sys.stderr)
        return 2

    output_dir = args.output_dir or os.path.join(
        "evidence", f"discovery-run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    os.makedirs(output_dir, exist_ok=True)
    trace_path = os.path.join(output_dir, "trace.json")
    print(f"Writing live trace + screenshots to: {output_dir}")

    if args.target:
        if args.no_login:
            username = password = None
        else:
            username = args.username or os.environ.get("TARGET_APP_USERNAME", "demo")
            password = args.password or os.environ.get("TARGET_APP_PASSWORD", "demo")
        result = run_discovery(
            goal=args.goal,
            base_url=args.target.rstrip("/"),
            start_path=args.start_path,
            username=username,
            password=password,
            max_steps=args.max_steps,
            headless=not args.headed,
            output_dir=output_dir,
            interactive=False,
        )
    else:
        known_targets = {
            "member/account/banking tasks on the CU Servicing Console (mock)": os.environ.get(
                "TARGET_APP_BASE_URL", "http://127.0.0.1:5055"
            )
        }
        result = run_task(
            goal=args.goal,
            known_targets=known_targets,
            username=args.username,
            password=args.password,
            max_steps=args.max_steps,
            headless=not args.headed,
            output_dir=output_dir,
            interactive=False,
        )
        if result.trace.routing:
            print(f"Routed to: {result.trace.routing['url']} ({result.trace.routing['reasoning']})")

    print(f"Status: {result.status}")
    if result.summary:
        print(f"Summary: {result.summary}")
    if result.outputs:
        print(f"Outputs: {json.dumps(result.outputs, indent=2)}")
    print(f"Trace saved to: {trace_path}")

    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
