"""Resolve which site a goal should run against, before any browser or
allowlist exists.

This is the piece that was missing for "go to OpenAI's trust center and
download compliance documents" to work without a human supplying the exact
URL: a goal about a known local target (the CU Servicing Console) commits
to it directly; anything else uses Anthropic's own server-side web_search
tool (no third-party search API needed -- see CLAUDE.md) to find a real
starting URL, then calls `navigate` to commit to it.

Why this doesn't touch the allowlist: routing is pure LLM reasoning (plus
Anthropic's hosted search) -- no Playwright page exists yet, so there's
nothing for a policy to restrict. The moment `navigate` is called, the
caller (`cua.agent.loop.run_task`) constructs a normal, locked
`AllowlistPolicy` for that one origin and everything downstream (browser
start, login, the click/type_text discovery loop) behaves exactly as it
already did for an explicitly-supplied target. The guardrail's shape
("one locked origin for the whole run") is preserved; only *which* origin
gets locked is now something the model can resolve itself instead of a
human typing it into a CLI flag.

Routing is intentionally fully autonomous here (CLAUDE.md: no pause for
human confirmation before committing to a routed target) -- the resolved
URL and the model's reasoning for it are still recorded on the
RoutingDecision for whatever evidence/trace wraps this, so the choice is
auditable after the fact even without a human in the loop at decision time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse

from anthropic import Anthropic

from cua.agent import usage as usage_module

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_ROUTING_TURNS = 5

ROUTING_TOOLS = [
    {"type": "web_search_20250305", "name": "web_search"},
    {
        "name": "navigate",
        "description": (
            "Call this once, when you've identified the correct starting URL for the goal. "
            "This commits to that site for the rest of the task -- don't call it until you're "
            "confident in the URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The exact starting URL, e.g. https://trust.openai.com/"},
                "reasoning": {"type": "string", "description": "Why this URL, in one sentence."},
            },
            "required": ["url", "reasoning"],
        },
    },
]


class RoutingError(RuntimeError):
    """Routing never committed to a URL within the turn budget."""


def _matches_known_target(url: str, known_targets: dict[str, str]) -> bool:
    netloc = urlparse(url).netloc
    return any(urlparse(known).netloc == netloc for known in known_targets.values())


@dataclass
class RoutingDecision:
    url: str
    reasoning: str
    # "known_target": matched a configured, pre-vetted entry exactly.
    # "web_search": confirmed via a live search this call actually made.
    # Never anything else -- a URL the model merely recalled from training
    # data, matching no known target and backed by no search, is rejected
    # and the model is made to search before it can commit (see the loop
    # below); routing is fully autonomous, so nothing else double-checks
    # an unverified guess before it's acted on.
    source: str
    usage: list[dict] = field(default_factory=list)


def _system_prompt(known_targets: dict[str, str]) -> str:
    if known_targets:
        known_lines = "\n".join(f"- {desc}: {url}" for desc, url in known_targets.items())
        known_block = (
            "You already have access to these local targets -- if the goal clearly matches one, "
            f"call navigate directly with its URL, no search needed:\n{known_lines}\n\n"
        )
    else:
        known_block = ""
    return (
        "You resolve which website a task should be performed against. You do not perform the "
        "task itself -- only decide the correct starting URL.\n\n"
        f"{known_block}"
        "If the goal doesn't match a known local target, use web_search to find the real, "
        "official URL for the site the goal describes (e.g. a company's trust center, security "
        "page, or support portal) -- prefer the company's own domain or a dedicated trust-portal "
        "subdomain over a third party discussing it. Once you're confident, call navigate with "
        "that URL and a one-sentence reason."
    )


def route_target(
    goal: str, known_targets: dict[str, str] | None = None, model: str | None = None
) -> RoutingDecision:
    """known_targets maps a short description -> base URL, e.g.
    {"member/account/banking tasks on the CU Servicing Console": "http://127.0.0.1:5055"}."""
    known_targets = known_targets or {}
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = Anthropic(default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None)
    model = model or DEFAULT_MODEL

    messages = [{"role": "user", "content": f"GOAL: {goal}"}]
    used_search = False
    usage_log: list[dict] = []

    for _ in range(MAX_ROUTING_TURNS):
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_system_prompt(known_targets),
            tools=ROUTING_TOOLS,
            tool_choice={"type": "any"},
            messages=messages,
        )
        usage_log.append(asdict(usage_module.record_usage(response, call_site="routing")))
        serialized = []
        navigate_input = None
        navigate_tool_use_id = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "navigate":
                navigate_input = block.input
                navigate_tool_use_id = block.id
                serialized.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            elif block.type == "text":
                serialized.append({"type": "text", "text": block.text})
            elif block.type == "server_tool_use":
                used_search = True
                serialized.append(block.model_dump())
            else:
                serialized.append(block.model_dump() if hasattr(block, "model_dump") else dict(block))
        messages.append({"role": "assistant", "content": serialized})

        if navigate_input is not None:
            url = navigate_input["url"]
            if _matches_known_target(url, known_targets):
                return RoutingDecision(
                    url=url, reasoning=navigate_input.get("reasoning", ""), source="known_target", usage=usage_log
                )
            if used_search:
                return RoutingDecision(
                    url=url, reasoning=navigate_input.get("reasoning", ""), source="web_search", usage=usage_log
                )
            # Routing is fully autonomous -- nothing double-checks this
            # choice before it's acted on. An unverified URL the model
            # merely recalled from training data (possibly stale, possibly
            # wrong) is not good enough to commit to on that basis alone;
            # require it to actually confirm via search first. Must answer
            # the navigate tool_use with a tool_result (not just a plain
            # message) or the *next* API call is rejected outright --
            # "tool_use ids were found without tool_result blocks" --
            # exactly the bug already fixed once in cua.agent.llm.
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": navigate_tool_use_id,
                            "content": (
                                f"Rejected: {url!r} has not been verified by an actual web_search "
                                "call. Call web_search to confirm this URL, then navigate again."
                            ),
                        }
                    ],
                }
            )
            continue

        messages.append(
            {"role": "user", "content": "Continue: call navigate once you've identified the right URL."}
        )

    raise RoutingError(f"did not commit to a URL within {MAX_ROUTING_TURNS} turns for goal: {goal!r}")
