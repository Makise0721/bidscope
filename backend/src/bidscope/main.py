"""FastAPI application factory and lifespan for BidScope.

The lifespan owns the long-lived resources every request shares: the async engine
and session factory, the demo query graph (fake model + hash embeddings), the
local object store, and the :class:`~bidscope.api.dependencies.RunService` that
wraps them. Test-only routes are registered only when ``app_mode == "test"``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from bidscope.api.dependencies import create_run_service
from bidscope.api.routes import (
    evaluations,
    events,
    inbox,
    reports,
    runs,
    sources,
    subscriptions,
    test_controls,
)
from bidscope.clock import SystemClock
from bidscope.config import Settings, get_settings
from bidscope.graph.executor import mark_stale_runs_retryable

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Initialize shared resources on startup and tear them down on shutdown."""
    settings: Settings = app.state.settings
    clock = SystemClock()
    async with create_run_service(settings, clock=clock) as (service, engine):
        app.state.clock = clock
        app.state.run_service = service
        app.state.engine = engine
        app.state.fail_next_node = False
        stale_before = clock.now() - timedelta(
            seconds=settings.stale_run_after_seconds
        )
        await mark_stale_runs_retryable(
            session_factory=service.session_factory,
            stale_before=stale_before,
        )
        try:
            yield
        finally:
            await service.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app, wiring routers and the lifespan."""
    resolved_settings = settings or get_settings()
    application = FastAPI(title="BidScope", version="0.1.0", lifespan=lifespan)
    application.state.settings = resolved_settings

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": resolved_settings.app_mode}

    application.include_router(runs.router)
    application.include_router(events.router)
    application.include_router(reports.router)
    application.include_router(subscriptions.router)
    application.include_router(inbox.router)
    application.include_router(sources.router)
    application.include_router(evaluations.router)
    # Test-controls routes are registered ONLY in test mode; in every other mode
    # requests to /api/test-controls/* fall through to a 404.
    if resolved_settings.app_mode == "test":
        application.include_router(test_controls.router)

    # Serve the built SPA. Mounted AFTER all API routes so it never shadows
    # them; the catch-all falls back to index.html for client-side routing.
    if STATIC_DIR.exists():
        application.mount(
            "/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets"
        )

        @application.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> FileResponse:  # noqa: ARG001
            index_file = STATIC_DIR / "index.html"
            if index_file.exists():
                return FileResponse(index_file)
            return FileResponse(STATIC_DIR / full_path)

    return application


app = create_app()
