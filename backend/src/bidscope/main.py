"""FastAPI application factory and lifespan for BidScope.

The lifespan owns the long-lived resources every request shares: the async engine
and session factory, the demo query graph (fake model + hash embeddings), the
local object store, and the :class:`~bidscope.api.dependencies.RunService` that
wraps them. Test-only routes are registered only when ``app_mode == "test"``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from bidscope.api.dependencies import create_run_service
from bidscope.api.routes import events, reports, runs, test_controls
from bidscope.config import Settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """Initialize shared resources on startup and tear them down on shutdown."""
    settings: Settings = app.state.settings
    service, engine = create_run_service(settings)
    app.state.run_service = service
    app.state.engine = engine
    app.state.fail_next_node = False
    yield
    await engine.dispose()


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
    # Test-controls routes are registered ONLY in test mode; in every other mode
    # requests to /api/test-controls/* fall through to a 404.
    if resolved_settings.app_mode == "test":
        application.include_router(test_controls.router)

    return application


app = create_app()
