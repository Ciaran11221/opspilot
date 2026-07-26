"""Tests for main.py's HTTP routes.

/api/chat mocks agent.run_agent rather than hitting the real Anthropic API -
verifying main.py correctly wraps whatever the agent loop yields as SSE is
main.py's job to test; whether the agent picks the right tool for a given
prompt is the eval harness's job (backend/evals/), not this suite's.
"""
from __future__ import annotations

from unittest.mock import patch

import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def test_health_check():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_heartbeat_updates_last_seen_time():
    before = main._last_heartbeat["t"]
    response = client.post("/api/heartbeat")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert main._last_heartbeat["t"] >= before


def test_upload_rejects_unknown_kind():
    response = client.post(
        "/api/upload",
        data={"kind": "not_a_real_kind"},
        files={"file": ("accounts.csv", b"Username,Status\nalice,Active\n", "text/csv")},
    )
    assert response.json() == {"error": "kind must be 'accounts' or 'tickets'"}


def test_upload_accounts_csv_creates_a_dataset():
    response = client.post(
        "/api/upload",
        data={"kind": "accounts"},
        files={"file": ("accounts.csv", b"Username,Status,Last Login\nalice,Active,2026-07-01\n", "text/csv")},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["kind"] == "accounts"
    assert body["row_count"] == 1
    assert "dataset_id" in body

    summary = client.get(f"/api/dataset/{body['dataset_id']}")
    assert summary.json()["accounts_loaded"] == 1


def test_upload_second_file_onto_existing_dataset_id_combines_them():
    first = client.post(
        "/api/upload",
        data={"kind": "accounts"},
        files={"file": ("accounts.csv", b"Username,Status\nalice,Active\n", "text/csv")},
    )
    dataset_id = first.json()["dataset_id"]

    second = client.post(
        "/api/upload",
        data={"kind": "tickets", "dataset_id": dataset_id},
        files={"file": ("tickets.csv", b"Key,Status,Priority\nOPS-1,Open,P1\n", "text/csv")},
    )
    assert second.json()["dataset_id"] == dataset_id

    summary = client.get(f"/api/dataset/{dataset_id}").json()
    assert summary["accounts_loaded"] == 1
    assert summary["tickets_loaded"] == 1


def test_get_dataset_summary_for_unknown_id_returns_error_body():
    response = client.get("/api/dataset/does-not-exist")
    assert response.json() == {"error": "dataset not found"}


def test_chat_streams_agent_events_as_sse_and_ends_with_done_sentinel():
    async def fake_run_agent(user_message, api_key, dataset_id):
        yield {"type": "plan", "text": "thinking", "turn": 0}
        yield {"type": "final", "text": "done"}

    with patch("main.run_agent", fake_run_agent):
        response = client.post("/api/chat", json={"message": "hi", "api_key": "test-key"})

    assert response.status_code == 200
    body = response.text
    assert '"type": "plan"' in body or '"type":"plan"' in body
    assert body.strip().endswith("data: [DONE]")


def test_chat_wraps_unexpected_exceptions_as_an_error_event():
    async def broken_run_agent(user_message, api_key, dataset_id):
        raise RuntimeError("boom")
        yield  # pragma: no cover - unreachable, makes this a generator

    with patch("main.run_agent", broken_run_agent):
        response = client.post("/api/chat", json={"message": "hi", "api_key": "test-key"})

    assert "boom" in response.text
    assert response.text.strip().endswith("data: [DONE]")


def test_chat_sync_returns_the_aggregated_result_as_plain_json():
    async def fake_run_agent_sync(message, api_key, dataset_id):
        return {"status": "ok", "answer": "done", "tool_calls": [], "draft_reports": []}

    with patch("main.run_agent_sync", fake_run_agent_sync):
        response = client.post("/api/chat-sync", json={"message": "hi", "api_key": "test-key"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "answer": "done", "tool_calls": [], "draft_reports": []}


def test_chat_sync_passes_through_an_error_result_with_200_status():
    # A run that ends in an agent-level error (bad key, MAX_TURNS, etc.) is
    # still a successful HTTP call - the error is data in the body
    # (status: "error"), not an HTTP-level failure. This matters for a
    # Power Automate flow calling this endpoint: it should branch on the
    # "status" field in the response, not on the HTTP status code.
    async def fake_run_agent_sync(message, api_key, dataset_id):
        return {"status": "error", "answer": None, "tool_calls": [], "draft_reports": [], "error": "boom"}

    with patch("main.run_agent_sync", fake_run_agent_sync):
        response = client.post("/api/chat-sync", json={"message": "hi", "api_key": "test-key"})

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error"] == "boom"
