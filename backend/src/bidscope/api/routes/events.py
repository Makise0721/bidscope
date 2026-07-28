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

from bidscope.api.auth import require_admin_token
from bidscope.api.dependencies import RunService
from bidscope.observability import METRICS_REGISTRY
from bidscope.persistence.models import RunEvent

router = APIRouter(
    tags=["events"], dependencies=[Depends(require_admin_token)]
)

#: Seconds between heartbeat comments while waiting for terminal state.
HEARTBEAT_INTERVAL = 15.0
#: Poll cadence for new events / terminal status. Kept short so a run that
#: reaches a terminal state is surfaced to the SSE client within ~POLL_INTERVAL
#: seconds rather than waiting up to HEARTBEAT_INTERVAL. The heartbeat comment
#: (which keeps proxies from dropping the idle connection) is still emitted at
#: HEARTBEAT_INTERVAL; the two cadences are deliberately decoupled.
POLL_INTERVAL = 0.5
#: Terminal/pausing statuses after which the stream closes. A run that pauses
#: at ``awaiting_confirmation`` is effectively terminal from the SSE client's
#: perspective: the run will not progress until ``POST /confirm`` resumes it, so
#: the stream emits a terminal marker (carrying the status) and closes. This
#: matches the web client's contract (see ``web/src/test/mockServer.ts``): the
#: Workbench's terminal handler maps the status to its ``awaiting_confirmation``
#: phase and renders the confirmation panel.
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "evidence_insufficient",
    "retryable",
    "awaiting_confirmation",
}


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

    max_connections = getattr(service.settings, "max_sse_connections", 100)
    active = int(getattr(request.app.state, "active_sse_connections", 0))
    if active >= max_connections:
        raise HTTPException(status_code=429, detail="sse connection capacity exhausted")
    request.app.state.active_sse_connections = active + 1
    METRICS_REGISTRY.increment_gauge("bidscope_sse_connections")

    async def event_generator() -> Any:
        after_seq = last_event_id
        next_heartbeat_at = asyncio.get_running_loop().time() + HEARTBEAT_INTERVAL
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
            # Wait briefly for new events. Poll tightly so terminal state is
            # surfaced quickly, and emit a heartbeat comment at the slower
            # HEARTBEAT_INTERVAL so idle connections are not dropped by proxies.
            now = asyncio.get_running_loop().time()
            if now >= next_heartbeat_at:
                yield {"comment": "heartbeat"}
                next_heartbeat_at = now + HEARTBEAT_INTERVAL
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                return

    async def tracked_generator() -> Any:
        try:
            async for item in event_generator():
                yield item
        finally:
            current = int(getattr(request.app.state, "active_sse_connections", 1))
            request.app.state.active_sse_connections = max(0, current - 1)
            METRICS_REGISTRY.decrement_gauge("bidscope_sse_connections")

    return EventSourceResponse(tracked_generator())


def _parse_last_event_id(request: Request) -> int:
    """Extract the resume ``seq`` to start streaming after.

    The primary source is the SSE ``Last-Event-ID`` header. Browser
    ``EventSource`` cannot set custom headers, so we also honour an
    ``?after_seq=`` query parameter as a fallback when the header is absent;
    the header always wins when both are present.
    """
    header = request.headers.get("Last-Event_ID") or request.headers.get("Last-Event-ID")
    if header is not None:
        try:
            return int(header)
        except ValueError:
            return -1
    query = request.query_params.get("after_seq")
    if query is not None:
        try:
            return int(query)
        except ValueError:
            return -1
    return -1
