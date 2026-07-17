"""Tests for agent.py's tool-use loop.

These mock anthropic.AsyncAnthropic entirely, so the loop's own control
flow (plan -> tool call -> result -> repeat -> final, MAX_TURNS cutoff,
error handling) is tested without calling the real API - no key, no cost,
safe to run in CI on every push. Anything that actually needs a real model
response (does it pick the right tool for a given prompt?) belongs in the
eval harness (backend/evals/), not here.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import agent
import anthropic


def text_block(text: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="text", text=text)


def tool_use_block(name: str, input: dict, id: str = "toolu_1") -> types.SimpleNamespace:
    return types.SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def fake_response(content: list, stop_reason: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


async def collect_events(user_message: str, api_key: str = "test-key", dataset_id=None) -> list[dict]:
    events = []
    async for event in agent.run_agent(user_message, api_key, dataset_id):
        events.append(event)
    return events


class TestRunAgentLoop:
    async def test_single_tool_call_then_final_answer(self):
        responses = [
            fake_response(
                [text_block("Let me check accounts."), tool_use_block("query_accounts", {"status": "ACTIVE"})],
                stop_reason="tool_use",
            ),
            fake_response([text_block("Found 3 active accounts.")], stop_reason="end_turn"),
        ]
        mock_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock(side_effect=responses)))

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = await collect_events("find active accounts")

        event_types = [e["type"] for e in events]
        assert event_types == ["plan", "tool_call", "tool_result", "final"]
        assert events[1]["name"] == "query_accounts"
        assert events[2]["result"]["count"] >= 0  # ran against real DEMO data, whatever it currently is
        assert events[3]["text"] == "Found 3 active accounts."

    async def test_multiple_tool_calls_in_one_turn(self):
        responses = [
            fake_response(
                [
                    tool_use_block("query_accounts", {"elevated_only": True}, id="t1"),
                    tool_use_block("query_tickets", {"status": "Open"}, id="t2"),
                ],
                stop_reason="tool_use",
            ),
            fake_response([text_block("done")], stop_reason="end_turn"),
        ]
        mock_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock(side_effect=responses)))

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = await collect_events("audit everything")

        tool_calls = [e for e in events if e["type"] == "tool_call"]
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert {e["name"] for e in tool_calls} == {"query_accounts", "query_tickets"}
        assert len(tool_results) == 2

    async def test_unknown_tool_name_surfaces_as_error_result_not_a_crash(self):
        responses = [
            fake_response([tool_use_block("delete_everything", {})], stop_reason="tool_use"),
            fake_response([text_block("ok")], stop_reason="end_turn"),
        ]
        mock_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock(side_effect=responses)))

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = await collect_events("do something unsupported")

        tool_result = next(e for e in events if e["type"] == "tool_result")
        assert "unknown tool" in tool_result["result"]["error"]

    async def test_tool_exception_surfaces_as_error_result_not_a_crash(self):
        responses = [
            fake_response(
                [tool_use_block("query_tickets", {"sla_risk_only": True, "sla_risk_threshold": "not-a-number"})],
                stop_reason="tool_use",
            ),
            fake_response([text_block("ok")], stop_reason="end_turn"),
        ]
        mock_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock(side_effect=responses)))

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = await collect_events("trigger a bad filter")

        tool_result = next(e for e in events if e["type"] == "tool_result")
        assert "error" in tool_result["result"]

    async def test_max_turns_cutoff_emits_error_instead_of_hanging(self):
        # Always responds with another tool call, never a final answer -
        # the loop must give up after MAX_TURNS rather than looping forever.
        always_tool_use = fake_response(
            [tool_use_block("query_accounts", {})], stop_reason="tool_use"
        )
        mock_client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=AsyncMock(return_value=always_tool_use))
        )

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = await collect_events("loop forever")

        assert events[-1]["type"] == "error"
        assert str(agent.MAX_TURNS) in events[-1]["text"]

    async def test_unknown_dataset_id_errors_without_calling_the_model(self):
        mock_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock()))

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = await collect_events("query my data", dataset_id="does-not-exist")

        assert len(events) == 1
        assert events[0]["type"] == "error"
        mock_client.messages.create.assert_not_called()

    async def test_authentication_error_surfaces_a_friendly_message(self):
        mock_request = types.SimpleNamespace(method="POST", url="https://api.anthropic.com/v1/messages")
        auth_error = anthropic.AuthenticationError(
            message="invalid x-api-key",
            response=types.SimpleNamespace(request=mock_request, status_code=401, headers={}),
            body=None,
        )
        mock_client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=AsyncMock(side_effect=auth_error))
        )

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = await collect_events("hello", api_key="bad-key")

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "API key" in events[0]["text"]

    async def test_empty_text_blocks_are_not_emitted_as_plan_events(self):
        # Some responses mix a tool_use block with a zero-length/whitespace
        # text block - that shouldn't produce a spurious empty plan event.
        responses = [
            fake_response(
                [text_block("   "), tool_use_block("query_accounts", {})],
                stop_reason="tool_use",
            ),
            fake_response([text_block("final")], stop_reason="end_turn"),
        ]
        mock_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=AsyncMock(side_effect=responses)))

        with patch("agent.anthropic.AsyncAnthropic", return_value=mock_client):
            events = await collect_events("test")

        plan_events = [e for e in events if e["type"] == "plan"]
        assert len(plan_events) == 0
