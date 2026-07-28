from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from bidscope.api.health import ReadinessProbe
from bidscope.config import Settings
from bidscope.delivery.objects import LocalObjectStore


class _Result:
    def scalar_one(self) -> int:
        return 1


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, statement: Any) -> _Result:
        assert str(statement) == "SELECT 1"
        return _Result()


class _Checkpoint:
    async def aget_tuple(self, config: dict[str, Any]) -> None:
        assert config == {"configurable": {"thread_id": "__bidscope_readiness__"}}
        return None


class _FailingCheckpoint:
    async def aget_tuple(self, _config: dict[str, Any]) -> None:
        raise RuntimeError(
            "checkpoint host=checkpoint.internal dsn=postgresql://user:secret@host/db"
        )


class _HangingCheckpoint:
    async def aget_tuple(self, _config: dict[str, Any]) -> None:
        await asyncio.sleep(10)


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


@pytest.mark.asyncio
async def test_readiness_reports_all_dependency_states(tmp_path: Path) -> None:
    settings = Settings(object_store_root=str(tmp_path / "objects"))
    result = await ReadinessProbe(timeout_seconds=0.1).check(
        settings=settings,
        session_factory=_SessionFactory(),
        checkpointer=_Checkpoint(),
        object_store=LocalObjectStore(settings.object_store_root),
    )

    assert result == {
        "status": "ok",
        "checks": {
            "database": {"status": "ok"},
            "checkpoint": {"status": "ok"},
            "object_store": {"status": "ok"},
            "configuration": {"status": "ok"},
        },
    }


@pytest.mark.asyncio
async def test_readiness_maps_timeout_to_stable_code_without_exception_details(
    tmp_path: Path,
) -> None:
    settings = Settings(object_store_root=str(tmp_path / "objects"))
    result = await ReadinessProbe(timeout_seconds=0.01).check(
        settings=settings,
        session_factory=_SessionFactory(),
        checkpointer=_HangingCheckpoint(),
        object_store=LocalObjectStore(settings.object_store_root),
    )

    assert result["status"] == "failed"
    assert result["checks"]["checkpoint"] == {
        "status": "failed",
        "code": "checkpoint_timeout",
    }
    assert "checkpoint.internal" not in str(result)
    assert "postgresql" not in str(result)
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_readiness_redacts_dependency_exception_messages(tmp_path: Path) -> None:
    settings = Settings(object_store_root=str(tmp_path / "objects"))
    result = await ReadinessProbe(timeout_seconds=0.1).check(
        settings=settings,
        session_factory=_SessionFactory(),
        checkpointer=_FailingCheckpoint(),
        object_store=LocalObjectStore(settings.object_store_root),
    )

    assert result["status"] == "failed"
    assert result["checks"]["checkpoint"] == {
        "status": "failed",
        "code": "checkpoint_unavailable",
    }
    assert "checkpoint.internal" not in str(result)
    assert "postgresql" not in str(result)
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_readiness_rejects_invalid_configuration_without_leaking_values(
    tmp_path: Path,
) -> None:
    settings = Settings(object_store_root=str(tmp_path / "objects"))
    settings.object_store_type = "s3"  # type: ignore[assignment]
    settings.s3_bucket = "private-bucket.internal"
    settings.s3_endpoint = "https://object-store.internal"

    result = await ReadinessProbe(timeout_seconds=0.1).check(
        settings=settings,
        session_factory=_SessionFactory(),
        checkpointer=_Checkpoint(),
        object_store=LocalObjectStore(settings.object_store_root),
    )

    assert result["status"] == "failed"
    assert result["checks"]["configuration"] == {
        "status": "failed",
        "code": "configuration_invalid",
    }
    assert "private-bucket.internal" not in str(result)
    assert "object-store.internal" not in str(result)
