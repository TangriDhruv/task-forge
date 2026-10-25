"""Anthropic-backed decision step of the observe -> decide -> act loop
(assignment.md §3.1). Uses Claude's tool-use API: every turn, the model is
forced (tool_choice="any") to call exactly one of the tools below, referencing
elements only by the 'ref' numbers in the observation it was just given.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from anthropic import Anthropic

from cua.agent import usage

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

TOOLS = [
    {
        "name": "click",
        "description": "Click a link, button, or checkbox identified by its 'ref' number from the current observation.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "integer", "description": "The 'ref' number of the element to click."}},
            "required": ["ref"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into a text field or textarea identified by 'ref'. Replaces any existing value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "integer"},
                "text": {"type": "string"},
                "submit": {
                    "type": "boolean",
                    "description": "If true, press Enter after typing (use for a search box with no visible submit button). Default false.",
                },
            },
            "required": ["ref", "text"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option in a dropdown ('combobox') identified by 'ref'. 'value' must be one of that element's listed option values.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "integer"}, "value": {"type": "string"}},
            "required": ["ref", "value"],
        },
    },
    {
        "name": "finish_success",
        "description": "Call once the goal is fully accomplished and verified on the current page. Provide the requested outputs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One sentence describing what was accomplished."},
                "outputs": {
                    "type": "object",
                    "description": "Key/value data the goal asked to extract or confirm (e.g. a ticket number, a balance, a status).",
                },
            },
            "required": ["summary", "outputs"],
        },
    },
    {
        "name": "request_human_help",
        "description": (
            "Call this when you need a human to do ONE specific thing you cannot or should not do "
            "yourself, after which you expect to keep going on the SAME task: enter a password or "
            "credential you don't have, solve a CAPTCHA or visual puzzle, or confirm a risky/"
            "ambiguous step before it proceeds. This pauses you and hands the live page to a human "
            "operator; you will be told when they're done and can continue from the resulting page. "
            "Do not use this for a hopeless dead end or a goal that looks impossible -- use "
            "finish_stuck for that instead, since this tool implies you expect to resume."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["credential", "captcha", "confirmation", "other"],
                    "description": "credential: you need a password/secret you don't have. captcha: a visual/puzzle challenge. confirmation: a risky step that should get a human yes/no. other: anything else that needs a person.",
                },
                "reason": {"type": "string", "description": "What you need the human to do, specifically, and why."},
                "field_ref": {
                    "type": "integer",
                    "description": "The 'ref' of the field involved (e.g. the password input), if any.",
                },
                "credential_name": {
                    "type": "string",
                    "description": (
                        "Only for kind=credential: a short, stable, descriptive name for this "
                        "credential (e.g. 'target_app_admin_password') so a previously-saved value "
                        "can be reused automatically next time instead of pausing again."
                    ),
                },
            },
            "required": ["kind", "reason"],
        },
    },
    {
        "name": "finish_stuck",
        "description": (
            "Call this if you cannot safely or confidently make progress: the goal looks impossible given what "
            "the page shows, a reasonable approach has already been tried and keeps failing, or something is "
            "risky or ambiguous enough that a human should decide. This raises a human-escalation request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

SYSTEM_PROMPT = """You are an automation agent operating a legacy, server-rendered internal web \
application on behalf of a human operator, the same way that operator would: by reading the page \
and clicking or typing on real controls. There is no API for this application.

Each turn you receive the current page as structured JSON: its URL, title, the visible text on \
the page, and a list of interactive elements, each with a 'ref' number. Act using exactly one of \
the provided tools, referencing elements only by 'ref' numbers from the CURRENT observation — refs \
are reassigned every turn and are not stable across turns, so never reuse a ref from an earlier turn.

Rules:
- Take one action per turn, then wait for the resulting observation before deciding the next one.
- Read the visible text carefully: it may show a validation error, an access-denied notice, a \
"not found" result, or a success confirmation. These are meaningful outcomes, not decoration.
- If a form submission is rejected (e.g. a validation error), read the message and correct the \
input rather than repeating the same action unchanged.
- If the goal is clearly impossible given what the page shows, or you do not know how to proceed \
safely, call finish_stuck with a clear reason rather than guessing or repeating a failing action.
- If you hit a password/credential field you have no value for, a CAPTCHA, or a step risky \
enough to need a yes/no from a person — call request_human_help rather than guessing, making \
one up, or skipping it. You will get control back once they're done.
- Only call finish_success once you can see, on the current page, clear confirmation that the \
goal was achieved. Include every piece of data the goal asked you to read or confirm in 'outputs'.
"""


def _serialize_content(content_blocks) -> list[dict]:
    serialized = []
    for block in content_blocks:
        if block.type == "text":
            serialized.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            serialized.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        else:
            serialized.append(block.model_dump() if hasattr(block, "model_dump") else dict(block))
    return serialized


class DecisionError(RuntimeError):
    pass


@dataclass
class Decision:
    tool_name: str
    tool_input: dict
    tool_use_id: str
    assistant_text: str | None


class Decider:
    """Owns the conversation history for one discovery run."""

    def __init__(self, goal: str, model: str | None = None, max_tokens: int = 1024):
        # A key not itself scoped to a workspace needs this header on every
        # request (see .env.example) -- harmless to send when unset-or-N/A.
        workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        default_headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self._client = Anthropic(default_headers=default_headers)
        self._model = model or DEFAULT_MODEL
        self._goal = goal
        self._max_tokens = max_tokens
        self.messages: list[dict] = []
        # Every API call this Decider makes, for cost/usage analysis (see
        # cua.agent.usage) -- also written to the cumulative ledger
        # immediately on each call, so a crashed/killed run doesn't lose
        # the record of a charge that already happened.
        self.usage_log: list[usage.UsageRecord] = []

    def _call_model(self) -> Decision:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            tool_choice={"type": "any"},
            messages=self.messages,
        )
        self.usage_log.append(usage.record_usage(response, call_site="discovery"))
        content = list(response.content)
        tool_use_index = next((i for i, b in enumerate(content) if b.type == "tool_use"), None)
        if tool_use_index is None:
            raise DecisionError("model did not return a tool call")

        # The loop executes exactly one action per turn (see SYSTEM_PROMPT),
        # but the model can still emit more than one tool_use block in a
        # single response despite tool_choice="any" only forcing *at least*
        # one -- observed in practice with extended thinking active. Keeping
        # an unexecuted tool_use in message history leaves it without a
        # matching tool_result, which the API rejects on the *next* call
        # ("tool_use ids were found without tool_result blocks"). Truncating
        # to the first tool_use keeps history internally consistent at the
        # cost of silently dropping any extra calls the model didn't need.
        kept = content[: tool_use_index + 1]
        self.messages.append({"role": "assistant", "content": _serialize_content(kept)})

        tool_use = kept[tool_use_index]
        assistant_text = next((b.text for b in kept if b.type == "text"), None)
        return Decision(tool_use.name, tool_use.input, tool_use.id, assistant_text)

    def start(self, observation_dict: dict) -> Decision:
        self.messages.append(
            {
                "role": "user",
                "content": f"GOAL: {self._goal}\n\nCurrent page:\n{json.dumps(observation_dict)}",
            }
        )
        return self._call_model()

    def respond(self, tool_use_id: str, result_dict: dict) -> Decision:
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(result_dict)}
                ],
            }
        )
        return self._call_model()
