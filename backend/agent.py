"""
OpsPilot agent loop.

This is the core "agentic" piece of the project: a plan -> tool call ->
result -> next step loop against the Claude API, using tool use. Every step
is yielded as a structured event so the frontend can render a live trace
panel instead of a spinner.

Design notes for whoever picks this up later:
- The loop is intentionally simple (no separate planner model, no
  multi-agent handoff) - it's Claude's native tool-use loop, made visible.
  That's the honest scope for an MVP demo; a "multi-agent" claim would not
  be accurate here and shouldn't be made in the README or CV.
- MAX_TURNS caps runaway loops during the demo.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from typing import Any, cast

import anthropic
import dataset_store
from anthropic import types as anthropic_types
from tools import DEMO_TOOL_IMPLEMENTATIONS, build_tool_implementations

MODEL = os.environ.get("OPSPILOT_MODEL", "claude-haiku-4-5-20251001")
MAX_TURNS = 6

SYSTEM_PROMPT = """You are OpsPilot, an IT-operations assistant with tools to inspect a synthetic \
account directory (Okta/M365-style) and a synthetic ticket queue (Jira-style), and to draft \
report/ticket artifacts.

Rules:
- Always use tools to gather facts before making claims about accounts or tickets. Never invent \
account names, ticket keys, or numbers.
- When asked to find a set of accounts or tickets meeting some criteria, call query_accounts or \
query_tickets with the narrowest filters that match the request, rather than pulling everything \
and filtering yourself in prose.
- Query results show at most 10 records in detail even when more match - the `count` field always \
reflects the true total, and a `note` will say so when the list was capped. Only draft artifacts \
for the records actually shown to you, never for records you haven't seen. If a query is capped and \
the user needs the rest, say so and suggest narrowing the filter rather than guessing at unseen data.
- When the user asks for a report, offboarding ticket, or similar artifact, gather the relevant \
data first, then call draft_report once per artifact needed - once for a single report, or once \
per item when asked to draft something "for each" account/ticket in a set.
- All data is synthetic and clearly labeled as such. Do not claim these are real user accounts or \
real support tickets, and do not claim any action here has been submitted to a real system - \
draft_report only produces a draft artifact for the demo.
- Keep prose between tool calls short - a sentence on what you found and what you'll do next. The \
trace panel is showing your steps live, so you don't need to repeat yourself.
"""

TOOLS = [
    {
        "name": "query_accounts",
        "description": (
            "Query the synthetic account directory (Okta/M365-style export) for accounts matching "
            "filters such as status, inactivity, or elevated permissions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Account status filter, e.g. 'ACTIVE'",
                },
                "min_inactive_days": {
                    "type": "integer",
                    "description": "Only return accounts whose last login is at least this many days ago",
                },
                "elevated_only": {
                    "type": "boolean",
                    "description": "Only return accounts with an admin-style title or elevated group membership",
                },
            },
        },
    },
    {
        "name": "query_tickets",
        "description": (
            "Query the synthetic ticket export (Jira-style) for tickets matching filters such as "
            "status, priority, or SLA risk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Ticket status filter, e.g. 'Open', 'In Progress', 'Resolved'",
                },
                "priority": {
                    "type": "string",
                    "description": "Priority filter, e.g. 'P1'",
                },
                "sla_risk_only": {
                    "type": "boolean",
                    "description": "Only return open tickets that have crossed sla_risk_threshold of their SLA window",
                },
                "sla_risk_threshold": {
                    "type": "number",
                    "description": "Fraction (0-1+) of SLA window elapsed to count as at-risk. Default 0.8.",
                },
            },
        },
    },
    {
        "name": "draft_report",
        "description": (
            "Draft an output artifact - an offboarding ticket, an SLA-risk summary report, or an "
            "account hygiene report - once enough data has been gathered. This is a draft only; "
            "it is not submitted to any real system."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "report_type": {
                    "type": "string",
                    "enum": ["offboarding_ticket", "sla_risk_report", "account_hygiene_report"],
                },
                "body_markdown": {
                    "type": "string",
                    "description": "The full drafted content, in markdown.",
                },
                "related_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Account ids and/or ticket keys this artifact references.",
                },
            },
            "required": ["title", "report_type", "body_markdown"],
        },
    },
]


def _event(event_type: str, **data: Any) -> dict[str, Any]:
    """Build a single trace event dict sent to the frontend over SSE.

    Args:
        event_type: One of ``"plan"``, ``"tool_call"``, ``"tool_result"``,
            ``"final"``, or ``"error"``.
        **data: Event-specific fields (e.g. ``text``, ``name``, ``input``,
            ``result``, ``turn``, ``tool_use_id``).

    Returns:
        A dict with a ``type`` key plus all the given fields, ready to be
        JSON-serialized as an SSE ``data:`` line.
    """
    return {"type": event_type, **data}


def _message(role: str, content: Any) -> anthropic_types.MessageParam:
    return cast(anthropic_types.MessageParam, {"role": role, "content": content})


async def run_agent(
    user_message: str, api_key: str, dataset_id: str | None = None
) -> AsyncGenerator[dict[str, Any], None]:
    """Run the tool-use loop for a single user message.

    Streams every step of the loop as a trace event so the frontend can
    render a live plan -> tool call -> result -> next step panel instead of
    a spinner:

    - ``plan``: assistant text explaining what it's about to do.
    - ``tool_call``: a tool name + its input arguments.
    - ``tool_result``: a tool name + its return value.
    - ``final``: the assistant's final text answer.
    - ``error``: something went wrong (bad API key, unknown dataset, etc.).

    Args:
        user_message: The user's chat input.
        api_key: An Anthropic API key, used to construct a per-request client.
        dataset_id: If provided, looks up an uploaded dataset via
            ``dataset_store`` and runs the agent against it instead of the
            bundled synthetic demo data.

    Yields:
        Trace event dicts, in order, as described above.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)
    messages: list[anthropic_types.MessageParam] = [_message("user", user_message)]

    system_prompt = SYSTEM_PROMPT
    if dataset_id:
        dataset = dataset_store.get_dataset(dataset_id)
        if dataset is None:
            yield _event("error", text="That uploaded dataset is no longer available - please re-upload.")
            return
        tool_implementations = build_tool_implementations(dataset["accounts"], dataset["tickets"])
        system_prompt = SYSTEM_PROMPT + (
            "\n\nNote: you are running against data the user uploaded themselves (not the built-in demo "
            "dataset). It has been normalized from their CSV but may have gaps - some records may be "
            "missing fields the export didn't include. Tool results may include a 'note' field flagging "
            "rows that were skipped for a given filter; mention this to the user if it's relevant to their "
            "question rather than silently ignoring it."
        )
    else:
        tool_implementations = DEMO_TOOL_IMPLEMENTATIONS

    try:
        for turn in range(MAX_TURNS):
            response = await client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                tools=cast(list[anthropic_types.ToolUnionParam], TOOLS),
                messages=messages,
            )

            assistant_content = []
            tool_calls = []

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    # Only surface text as a "plan" step when this turn goes on
                    # to make more tool calls. When stop_reason isn't tool_use,
                    # this text *is* the final answer and is emitted once,
                    # below, as a "final" event - emitting it here too would
                    # show the same text twice in the trace panel.
                    if response.stop_reason == "tool_use":
                        yield _event("plan", text=block.text.strip(), turn=turn)
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    yield _event("tool_call", name=block.name, input=block.input, turn=turn, tool_use_id=block.id)
                    tool_calls.append(block)
                    assistant_content.append(
                        {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                    )

            messages.append(_message("assistant", assistant_content))

            if response.stop_reason == "max_tokens":
                # The response was cut off mid-generation, not a genuine "I'm
                # done" signal - stop_reason == "tool_use" and stop_reason ==
                # "end_turn" are both real completion states, but max_tokens
                # means Claude ran out of room, possibly mid-tool-call. Any
                # tool_use blocks above that DID fully parse (e.g. the first
                # 5 of 6 draft_report calls) still represent real, useful
                # work - execute those rather than silently discarding them -
                # but this must NOT be reported as a successful final answer,
                # since work may be incomplete (e.g. only 5 of 12 requested
                # tickets got drafted).
                for call in tool_calls:
                    impl = tool_implementations.get(call.name)
                    if impl is None:
                        result = {"error": f"unknown tool {call.name}"}
                    else:
                        try:
                            result = impl(**call.input)
                        except Exception as exc:  # e.g. a truncated call missing a required field
                            result = {"error": str(exc)}
                    yield _event("tool_result", name=call.name, result=result, turn=turn, tool_use_id=call.id)
                yield _event(
                    "error",
                    text=(
                        "Response was cut off before finishing (hit the model's output limit). "
                        f"{len(tool_calls)} tool call(s) above completed, but the task may be "
                        "incomplete - try asking for a smaller batch at once."
                    ),
                )
                return

            if response.stop_reason != "tool_use":
                final_text = "".join(b.text for b in response.content if b.type == "text")
                yield _event("final", text=final_text)
                return

            tool_result_content = []
            for call in tool_calls:
                impl = tool_implementations.get(call.name)
                if impl is None:
                    result = {"error": f"unknown tool {call.name}"}
                else:
                    try:
                        result = impl(**call.input)
                    except Exception as exc:  # surfaced to the trace panel, not swallowed
                        result = {"error": str(exc)}

                yield _event("tool_result", name=call.name, result=result, turn=turn, tool_use_id=call.id)
                tool_result_content.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(result),
                })

            messages.append(_message("user", tool_result_content))

        yield _event("error", text=f"Stopped after {MAX_TURNS} turns without a final answer.")

    except anthropic.AuthenticationError:
        yield _event("error", text="Invalid or missing API key. Enter a valid Anthropic API key to run the agent.")
    except Exception as exc:
        yield _event("error", text=f"Agent error: {exc}")
