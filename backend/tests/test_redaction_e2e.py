"""End-to-end regression test: real PII must never appear in what actually
gets sent to the Claude API.

test_redact.py checks RedactionMap and the redact_* helpers in isolation.
This file closes the gap those unit tests can't: it runs the real
run_agent loop (mocking only the Anthropic client, same pattern as
test_agent.py) and inspects the *actual arguments* passed to
client.messages.create on every turn - the literal payload that would go
over the wire - asserting a known real PII value never appears in it,
across every turn of a multi-turn tool-calling conversation.

This is the direct, automated version of the manual check described in
README's Honesty notes: open the browser Network tab and confirm the
outbound request body contains tokens like [PERSON_1], not real names.
"""
from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, patch

import dataset_store
import tools


def text_block(text: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="text", text=text)


def tool_use_block(name: str, input: dict, id: str = "toolu_1") -> types.SimpleNamespace:
    return types.SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def fake_response(content: list, stop_reason: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


def _flatten_call_payload(call) -> str:
    """Serialize everything passed to one client.messages.create() call
    (system blocks + messages, including nested tool_result JSON strings)
    into a single string, so a substring check covers the whole payload."""
    kwargs = call.kwargs
    parts = [json.dumps(kwargs.get("system", "")), json.dumps(kwargs.get("messages", []))]
    return " ".join(parts)


class TestNoRealPiiReachesTheModel:
    async def test_demo_dataset_account_pii_never_sent_to_api(self):
        # A real account from the bundled demo dataset - whatever its real
        # displayName/email currently are, they must never appear in any
        # payload sent to the API across the whole run.
        real_account = tools.DEMO_ACCOUNTS[0]
        real_display_name = real_account["displayName"]
        real_email = real_account["email"]

        responses = [
            fake_response(
                [tool_use_block("query_accounts", {"status": real_account["status"]})],
                stop_reason="tool_use",
            ),
            fake_response([text_block("Found the accounts you asked about.")], stop_reason="end_turn"),
        ]
        mock_create = AsyncMock(side_effect=responses)
        mock_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=mock_create))

        import agent

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = [e async for e in agent.run_agent("find accounts", "test-key")]

        # Sanity check the run actually exercised the tool loop and reached
        # a final answer - a test that short-circuits on an error proves
        # nothing about redaction.
        assert [e["type"] for e in events] == ["tool_call", "tool_result", "final"]

        for call in mock_create.await_args_list:
            payload = _flatten_call_payload(call)
            assert real_display_name not in payload, "real displayName leaked into an API call payload"
            assert real_email not in payload, "real email leaked into an API call payload"

        # The rehydrated version shown to the human IS allowed (expected) to
        # contain the real value - that's the whole point of rehydration.
        # If this account matched the query, it should show up in the human-
        # facing tool_result event in its real form.
        tool_result_event = next(e for e in events if e["type"] == "tool_result")
        shown_accounts = tool_result_event["result"].get("accounts", [])
        if any(acc.get("id") == real_account["id"] for acc in shown_accounts):
            shown = next(acc for acc in shown_accounts if acc["id"] == real_account["id"])
            assert shown["displayName"] == real_display_name

    async def test_uploaded_dataset_ticket_free_text_pii_never_sent_to_api(self):
        # Free-text ticket summaries are the harder case (test_redact.py's
        # test_redact_free_text_does_not_catch_unseen_name documents the
        # scope limit) - this confirms the easy, in-scope case actually
        # holds end to end: a name that DOES also appear in a structured
        # field (assignee) must be scrubbed from the summary too, and must
        # never reach the API in either place.
        dataset_id = dataset_store.create_dataset()
        real_name = "Aoife Nolan"
        dataset_store.set_accounts(dataset_id, [], meta={})
        dataset_store.set_tickets(
            dataset_id,
            [
                {
                    "key": "OPS-99",
                    "status": "Open",
                    "priority": "P2",
                    "assignee": real_name,
                    "summary": f"Escalated by {real_name} - customer needs urgent callback.",
                }
            ],
            meta={},
        )

        responses = [
            fake_response([tool_use_block("query_tickets", {"status": "Open"})], stop_reason="tool_use"),
            fake_response([text_block("Found the open ticket.")], stop_reason="end_turn"),
        ]
        mock_create = AsyncMock(side_effect=responses)
        mock_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=mock_create))

        import agent

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = [e async for e in agent.run_agent("find open tickets", "test-key", dataset_id=dataset_id)]

        assert [e["type"] for e in events] == ["tool_call", "tool_result", "final"]

        for call in mock_create.await_args_list:
            payload = _flatten_call_payload(call)
            assert real_name not in payload, "real name leaked into an API call payload (structured or free text)"

        # Rehydrated for the human: both the assignee field and the summary
        # prose should show the real name again.
        tool_result_event = next(e for e in events if e["type"] == "tool_result")
        shown_ticket = tool_result_event["result"]["tickets"][0]
        assert shown_ticket["assignee"] == real_name
        assert real_name in shown_ticket["summary"]
