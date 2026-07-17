"""OpsPilot backend entrypoint.

Routes served:
    GET  /                    - the static frontend (plain HTML, no Node build step)
    GET  /api/health          - basic liveness check
    POST /api/heartbeat       - keeps the server alive; see auto-shutdown below
    POST /api/shutdown        - explicit shutdown, fired when the browser tab closes
    POST /api/upload          - ingest an accounts/tickets CSV into a dataset
    GET  /api/dataset/{id}    - summary of an uploaded dataset
    POST /api/chat            - runs the agent loop, streams trace events as SSE

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8420

Then open http://localhost:8420. For a no-terminal demo experience, use
../start_opspilot.bat instead, which also enables auto-shutdown (see below).
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import csv_ingest
import dataset_store
from agent import run_agent
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="OpsPilot")

# --- Auto-shutdown -----------------------------------------------------
# Opt-in via the OPSPILOT_AUTO_SHUTDOWN env var (set by start_opspilot.bat)
# so a normal `uvicorn --reload` development session is never killed just
# because the browser tab happens to be closed for a minute. When enabled:
#   - the frontend POSTs /api/heartbeat every few seconds while its tab is open
#   - closing the tab fires an immediate /api/shutdown via navigator.sendBeacon
#   - a background thread also force-kills the process if heartbeats stop
#     arriving (covers a browser crash / force-quit, not just a clean close)
AUTO_SHUTDOWN: bool = os.environ.get("OPSPILOT_AUTO_SHUTDOWN") == "1"
_last_heartbeat: dict[str, float] = {"t": time.time()}
_HEARTBEAT_TIMEOUT = 8  # seconds without a heartbeat before we shut down


def _watch_for_disconnect() -> None:
    """Background loop: force-exit if no heartbeat arrives within the timeout.

    Runs as a daemon thread only when AUTO_SHUTDOWN is enabled. This is the
    fallback path for an unclean disconnect (browser crash, force-quit) -
    the common case (closing the tab normally) is handled instantly by the
    /api/shutdown beacon instead.
    """
    while True:
        time.sleep(2)
        if time.time() - _last_heartbeat["t"] > _HEARTBEAT_TIMEOUT:
            print("OpsPilot: no browser tab detected, shutting down.")
            os._exit(0)


if AUTO_SHUTDOWN:
    threading.Thread(target=_watch_for_disconnect, daemon=True).start()

# Static frontend build output. Not present during backend-only development
# unless frontend/dist exists - the API still works standalone in that case.
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


class ChatRequest(BaseModel):
    """Request body for POST /api/chat."""

    message: str
    api_key: str
    dataset_id: str | None = None


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@app.post("/api/heartbeat")
async def heartbeat() -> dict[str, bool]:
    """Record that a browser tab is still open (see AUTO_SHUTDOWN above)."""
    _last_heartbeat["t"] = time.time()
    return {"ok": True}


@app.post("/api/shutdown")
async def shutdown() -> dict[str, bool]:
    """Explicit shutdown request, fired via sendBeacon when the tab closes.

    Responds immediately, then exits shortly after on a short timer so the
    HTTP response actually has a chance to flush to the client first.
    """
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return {"ok": True}


@app.post("/api/upload")
async def upload_csv(
    kind: str = Form(...),
    dataset_id: str | None = Form(None),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Ingest an uploaded CSV into a dataset.

    Args:
        kind: Either ``"accounts"`` or ``"tickets"`` - which schema to
            normalize the file against.
        dataset_id: An existing dataset id to add this file to (so an
            accounts CSV and a tickets CSV can be combined into one
            dataset), or omitted to create a new dataset.
        file: The uploaded CSV file.

    Returns:
        On success: ``dataset_id``, ``kind``, ``row_count``,
        ``column_mapping`` (see ``csv_ingest._match_headers``), and
        ``warnings`` describing anything that couldn't be confidently parsed.
        On failure: ``{"error": ...}``.
    """
    if kind not in ("accounts", "tickets"):
        return {"error": "kind must be 'accounts' or 'tickets'"}

    contents = await file.read()
    result = (
        csv_ingest.ingest_accounts_csv(contents)
        if kind == "accounts"
        else csv_ingest.ingest_tickets_csv(contents)
    )

    if not dataset_id:
        dataset_id = dataset_store.create_dataset()

    meta = {
        "filename": file.filename,
        "row_count": result["row_count"],
        "column_mapping": result["column_mapping"],
        "warnings": result["warnings"],
    }
    if kind == "accounts":
        dataset_store.set_accounts(dataset_id, result["records"], meta)
    else:
        dataset_store.set_tickets(dataset_id, result["records"], meta)

    return {
        "dataset_id": dataset_id,
        "kind": kind,
        "row_count": result["row_count"],
        "column_mapping": result["column_mapping"],
        "warnings": result["warnings"],
    }


@app.get("/api/dataset/{dataset_id}")
async def get_dataset_summary(dataset_id: str) -> dict[str, Any]:
    """Return a summary of an uploaded dataset (row counts, ingestion metadata).

    Args:
        dataset_id: The dataset's UUID string, as returned by ``/api/upload``.

    Returns:
        The dataset summary dict (see ``dataset_store.describe``), or
        ``{"error": "dataset not found"}`` if the id doesn't exist.
    """
    summary = dataset_store.describe(dataset_id)
    if summary is None:
        return {"error": "dataset not found"}
    return summary


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    """Run the agent loop for one message and stream its trace as SSE.

    Each yielded event from ``agent.run_agent`` is forwarded to the client
    as an SSE ``data:`` line, terminated by a final ``[DONE]`` sentinel so
    the frontend knows the stream is complete.

    Args:
        req: The chat request body (message, API key, optional dataset id).
        request: The underlying FastAPI request, used to detect early
            client disconnects and stop streaming promptly.

    Returns:
        A ``text/event-stream`` response.
    """

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            async for event in run_agent(req.message, req.api_key, req.dataset_id):
                yield f"data: {json.dumps(event)}\n\n"
                if await request.is_disconnected():
                    break
        except Exception as exc:  # surfaced to the trace panel, not swallowed
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# Serve the built frontend (static files) if present. During backend-only
# development this directory may not exist yet - that's fine, the API still
# works and can be hit directly (e.g. from a dev frontend on another port).
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
