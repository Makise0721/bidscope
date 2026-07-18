from fastapi import FastAPI

from bidscope.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    application = FastAPI(title="BidScope", version="0.1.0")

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": resolved_settings.app_mode}

    return application


app = create_app()
