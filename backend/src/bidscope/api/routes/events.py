"""Server-Sent Events route for run progress.

``GET /api/runs/{id}/events`` streams the persisted ``run_events`` rows in
``seq`` order. It honours ``Last-Event-ID`` (resume after a given ``seq``),
emits a periodic heartbeat, and closes after the terminal event so clients can
detach without hanging.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from bidscope.api.dependencies import RunService
from bidscope.persistence.models import RunEvent

router = APIRouter(tags=["events"])

#: Seconds between heartbeat comments while waiting for terminal state.
HEARTBEAT_INTERVAL = 15.0
#: Terminal statuses after which the stream closes.
TERMINAL_STATUSES = {"completed", "failed", "evidence_insufficient", "retryable"}


def get_run_service(request: Request) -> RunService:
    from typing import cast

    return cast(RunService, request.app.state.run_service)


def _event_payload(event: RunEvent) -> dict[str, Any]:
    """Shape a RunEvent into the JSON data payload for an SSE message."""
    return {
        "seq": event.seq,
        "node": event.node,
        "event": event.event,
        "status": event.status,
        "message": event.message,
        "details": event.details,
    }


@router.get("/api/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    service: RunService = Depends(get_run_service),
) -> EventSourceResponse:
    """Stream run events as SSE, honouring Last-Event-ID and heartbeats."""
    run = await service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    last_event_id = _parse_last_event_id(request)

    async def event_generator() -> Any:
        after_seq = last_event_id
        while True:
            events = await service.list_events(run_id, after_seq=after_seq)
            for event in events:
                payload = json.dumps(_event_payload(event))
                yield {"id": str(event.seq), "event": event.event, "data": payload}
                after_seq = event.seq
            # Terminal run: emit a final terminal marker and close.
            fresh = await service.get_run(run_id)
            if fresh is not None and fresh.status in TERMINAL_STATUSES:
                payload = json.dumps({"status": fresh.status, "terminal": True})
                yield {"id": "terminal", "event": "terminal", "data": payload}
                return
            # Otherwise wait briefly for new events (with a heartbeat).
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except asyncio.CancelledError:
                return

    return EventSourceResponse(event_generator())


def _parse_last_event_id(request: Request) -> int:
    """Extract the resume ``seq`` from the ``Last-Event-ID`` header (default -1)."""
    header = request.headers.get("Last-Event_ID") or request.headers.get("Last-Event-ID")
    if header is None:
        return -1
    try:
        return int(header)
    except ValueError:
        return -1
