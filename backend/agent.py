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
from redact import RedactionMap, redact_accounts, redact_tickets
from tools import DEMO_ACCOUNTS, DEMO_TICKETS, NOW_DEMO, build_tool_implementations

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
        # Marks the end of the tools block as a cache breakpoint. The tools
        # array is byte-identical on every turn of every request, so caching
        # it here means turns 2+ (and subsequent requests within the cache
        # TTL) read it from cache instead of paying full input-token price
        # for these schemas every single call.
        "cache_control": {"type": "ephemeral"},
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

    # One RedactionMap per request: real PII (emails, usernames, display
    # names, assignees) is tokenized before the dataset is ever handed to
    # the tool implementations, so Claude only ever reasons over tokens -
    # never real values. The same map rehydrates tokens back to real values
    # in every event yielded to the frontend, below. Fresh per request (not
    # cached at module scope) so token assignment can't leak between users.
    rmap = RedactionMap()

    # System prompt as a list of cacheable content blocks rather than one
    # string. SYSTEM_PROMPT is byte-identical across every request (demo or
    # uploaded) - own cache breakpoint, hit regardless of dataset. The
    # redaction-token note is ALSO always present, in both branches below:
    # both the demo dataset and any uploaded dataset get redacted (see
    # redact_accounts/redact_tickets calls below), so Claude always needs to
    # know what [PERSON_1]-style tokens mean, not just on the upload path.
    # (Regression note: an earlier version only added this note for uploaded
    # datasets, even though demo-dataset queries were also redacted - Claude
    # would then see undefined tokens and describe them, confusingly, as
    # "anonymized" in its own final answer, which then got rehydrated back to
    # the real value - producing a self-contradictory sentence. Caught via
    # the eval harness's nonexistent_account_not_fabricated scenario.)
    redaction_note = {
        "type": "text",
        "text": (
            "Names, emails, and usernames in the data you can query have been replaced with tokens like "
            "[PERSON_1] and [EMAIL_2] before reaching you - this is a privacy layer, not missing or malformed "
            "data, and it applies to both the bundled demo dataset and any uploaded dataset. Always use these "
            "tokens exactly as given (in filters, in drafted reports, in your own answers) rather than "
            "inventing or guessing a real name or address, and don't describe them to the user as anonymized, "
            "redacted, or unusual - they are rehydrated back to real values for the human user automatically "
            "after you respond, so from the user's point of view nothing looks anonymized at all."
        ),
        "cache_control": {"type": "ephemeral"},
    }
    system_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        redaction_note,
    ]
    if dataset_id:
        dataset = dataset_store.get_dataset(dataset_id)
        if dataset is None:
            yield _event("error", text="That uploaded dataset is no longer available - please re-upload.")
            return
        redacted_accounts = redact_accounts(dataset["accounts"], rmap)
        redacted_tickets = redact_tickets(dataset["tickets"], rmap)
        tool_implementations = build_tool_implementations(redacted_accounts, redacted_tickets)
        system_blocks.append({
            "type": "text",
            "text": (
                "Note: you are running against data the user uploaded themselves (not the built-in demo "
                "dataset). It has been normalized from their CSV but may have gaps - some records may be "
                "missing fields the export didn't include. Tool results may include a 'note' field flagging "
                "rows that were skipped for a given filter; mention this to the user if it's relevant to "
                "their question rather than silently ignoring it."
            ),
            "cache_control": {"type": "ephemeral"},
        })
    else:
        redacted_accounts = redact_accounts(DEMO_ACCOUNTS, rmap)
        redacted_tickets = redact_tickets(DEMO_TICKETS, rmap)
        tool_implementations = build_tool_implementations(redacted_accounts, redacted_tickets, NOW_DEMO)



    try:
        for turn in range(MAX_TURNS):
            response = await client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=cast(list[anthropic_types.TextBlockParam], system_blocks),
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
                        yield _event("plan", text=rmap.rehydrate(block.text.strip()), turn=turn)
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    yield _event(
                        "tool_call", name=block.name, input=rmap.rehydrate(block.input), turn=turn, tool_use_id=block.id
                    )
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
                    yield _event(
                        "tool_result", name=call.name, result=rmap.rehydrate(result), turn=turn, tool_use_id=call.id
                    )
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
                yield _event("final", text=rmap.rehydrate(final_text))
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

                yield _event(
                    "tool_result", name=call.name, result=rmap.rehydrate(result), turn=turn, tool_use_id=call.id
                )
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


async def run_agent_sync(user_message: str, api_key: str, dataset_id: str | None = None) -> dict[str, Any]:
    """Run the agent loop to completion and return one aggregated JSON result.

    ``run_agent`` streams trace events for the browser's live trace panel -
    the right shape for a human watching a demo, but not what a Power
    Automate flow or Copilot Studio topic action can consume: those callers
    make one HTTP request and expect one JSON response back, not an SSE
    stream. This wraps the same underlying loop and collects every event
    into a single summary, so it's the entry point a Power Platform custom
    connector should call rather than ``run_agent`` directly.

    Args:
        user_message: The user's chat input.
        api_key: An Anthropic API key, used to construct a per-request client.
        dataset_id: If provided, runs against that uploaded dataset instead
            of the bundled demo data (see ``run_agent``).

    Returns:
        A dict with:
            - ``status``: ``"ok"`` or ``"error"``.
            - ``answer``: the final answer text, or ``None`` if the run
              ended in an error before producing one.
            - ``tool_calls``: list of ``{"name": ..., "input": ...}`` for
              every tool call made, in order - useful for a flow that wants
              to log or branch on what the agent actually did.
            - ``draft_reports``: list of every ``draft_report`` tool's
              result dict (title, reportType, body, relatedIds, draftedAt,
              status) - the artifacts a Power Automate flow would actually
              post into a ticketing system or Teams channel.
            - ``error``: the error text if ``status`` is ``"error"``,
              otherwise omitted.
    """
    tool_calls: list[dict[str, Any]] = []
    draft_reports: list[dict[str, Any]] = []
    answer: str | None = None
    error_text: str | None = None

    async for event in run_agent(user_message, api_key, dataset_id):
        if event["type"] == "tool_call":
            tool_calls.append({"name": event["name"], "input": event["input"]})
        elif event["type"] == "tool_result" and event["name"] == "draft_report":
            result = event["result"]
            if "error" not in result:
                draft_reports.append(result)
        elif event["type"] == "final":
            answer = event["text"]
        elif event["type"] == "error":
            error_text = event["text"]

    if error_text is not None:
        return {
            "status": "error",
            "answer": None,
            "tool_calls": tool_calls,
            "draft_reports": draft_reports,
            "error": error_text,
        }
    return {
        "status": "ok",
        "answer": answer,
        "tool_calls": tool_calls,
        "draft_reports": draft_reports,
    }