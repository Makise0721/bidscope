"""Bounded, sanitized dependency readiness probes."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypedDict

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bidscope.config import Settings
from bidscope.delivery.objects import LocalObjectStore, S3ObjectStore


class CheckState(TypedDict, total=False):
    status: str
    code: str


class ReadinessResult(TypedDict):
    status: str
    checks: dict[str, CheckState]


_CHECK_NAMES = ("database", "checkpoint", "object_store", "configuration")
_FAILURE_CODES = frozenset(
    {
        "database_unavailable",
        "database_timeout",
        "checkpoint_unavailable",
        "checkpoint_timeout",
        "object_store_unavailable",
        "object_store_timeout",
        "configuration_invalid",
    }
)
_DEFAULT_TIMEOUT_SECONDS = 2.0
_MAX_TIMEOUT_SECONDS = 5.0
_READINESS_THREAD_ID = "__bidscope_readiness__"


class ReadinessProbe:
    """Run dependency checks with a bounded duration and stable output."""

    def __init__(self, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = max(0.001, min(timeout_seconds, _MAX_TIMEOUT_SECONDS))

    async def check(
        self,
        *,
        settings: Settings,
        session_factory: Any,
        checkpointer: Any,
        object_store: Any,
    ) -> ReadinessResult:
        checks: dict[str, CheckState] = {
            "database": await self._run(
                "database", self._database, session_factory
            ),
            "checkpoint": await self._run(
                "checkpoint", self._checkpoint, checkpointer
            ),
            "object_store": await self._run(
                "object_store", self._object_store, object_store
            ),
            "configuration": await self._run(
                "configuration", self._configuration, settings
            ),
        }
        return {
            "status": "ok" if all(item["status"] == "ok" for item in checks.values()) else "failed",
            "checks": checks,
        }

    async def _run(
        self,
        name: str,
        probe: Callable[[Any], Awaitable[None]],
        dependency: Any,
    ) -> CheckState:
        if dependency is None:
            return {"status": "failed", "code": f"{name}_unavailable"}
        try:
            await asyncio.wait_for(probe(dependency), timeout=self.timeout_seconds)
        except TimeoutError:
            return {"status": "failed", "code": f"{name}_timeout"}
        except Exception:
            code = "configuration_invalid" if name == "configuration" else f"{name}_unavailable"
            return {"status": "failed", "code": code}
        return {"status": "ok"}

    @staticmethod
    async def _database(session_factory: Any) -> None:
        async with session_factory() as session:
            await session.execute(sa.text("SELECT 1"))

    @staticmethod
    async def _checkpoint(checkpointer: Any) -> None:
        await checkpointer.aget_tuple(
            {"configurable": {"thread_id": _READINESS_THREAD_ID}}
        )

    @staticmethod
    async def _object_store(object_store: Any) -> None:
        if isinstance(object_store, LocalObjectStore):
            root = Path(object_store.root)
            if not root.is_dir() or not os.access(root, os.W_OK):
                raise OSError("object store root is unavailable")
            return

        if isinstance(object_store, S3ObjectStore):
            await asyncio.to_thread(
                object_store.client.head_bucket,
                Bucket=object_store.bucket,
            )
            return

        head_bucket = getattr(object_store, "head_bucket", None)
        if head_bucket is None:
            raise TypeError("unsupported object store")
        result = head_bucket()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def _configuration(settings: Settings) -> None:
        # Re-validate the already loaded settings so a mutated app state cannot
        # report ready with an invalid storage configuration.
        Settings.model_validate(settings.model_dump())


router = APIRouter()


def _failed_result() -> ReadinessResult:
    return {
        "status": "failed",
        "checks": {
            name: {"status": "failed", "code": f"{name}_unavailable"}
            for name in _CHECK_NAMES
        },
    }


def _sanitize_result(value: Any) -> ReadinessResult:
    """Keep the public response to a fixed, non-sensitive schema."""
    if not isinstance(value, Mapping):
        return _failed_result()
    raw_checks = value.get("checks")
    if not isinstance(raw_checks, Mapping):
        return _failed_result()

    checks: dict[str, CheckState] = {}
    for name in _CHECK_NAMES:
        raw_check = raw_checks.get(name)
        if not isinstance(raw_check, Mapping) or raw_check.get("status") != "ok":
            code = raw_check.get("code") if isinstance(raw_check, Mapping) else None
            if not isinstance(code, str) or code not in _FAILURE_CODES:
                code = f"{name}_unavailable"
            checks[name] = {"status": "failed", "code": code}
        else:
            checks[name] = {"status": "ok"}
    status = "ok" if all(item["status"] == "ok" for item in checks.values()) else "failed"
    return {"status": status, "checks": checks}


@router.get("/readyz", include_in_schema=True)
async def readyz(request: Request) -> JSONResponse:
    """Return process readiness without exposing deployment details."""
    settings = getattr(request.app.state, "settings", None)
    service = getattr(request.app.state, "run_service", None)
    probe = getattr(request.app.state, "readiness_probe", ReadinessProbe())
    checkpointer = getattr(request.app.state, "checkpointer", None)
    session_factory = getattr(service, "session_factory", None)
    object_store = getattr(service, "object_store", None)

    if settings is None or service is None:
        result = _failed_result()
    else:
        try:
            result = _sanitize_result(
                await probe.check(
                    settings=settings,
                    session_factory=session_factory,
                    checkpointer=checkpointer,
                    object_store=object_store,
                )
            )
        except Exception:
            result = _failed_result()

    return JSONResponse(
        status_code=200 if result["status"] == "ok" else 503,
        content=result,
    )


__all__ = ["ReadinessProbe", "ReadinessResult", "readyz", "router"]
