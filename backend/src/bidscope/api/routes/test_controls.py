"""Test-only control routes.

These routes are registered *only* when ``app_mode == "test"`` and are protected
by a separate ``X-Test-Control-Token`` header. They expose bounded, one-shot
controls used by the integration test suite:

* ``POST /api/test-controls/fail-next-node`` — inject a single deterministic
  node failure (for recovery/retry regression tests).
* ``POST /api/test-controls/import-batch-2`` — trigger import of the
  synthetic-demo Batch 2 bundle.

In demo, development, or production mode these routes are never registered, so
requests return 404.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bidscope.persistence.repositories import SnapshotRepository
from bidscope.snapshots.importer import SnapshotImporter

router = APIRouter(prefix="/api/test-controls", tags=["test-controls"])

#: Relative path from the project root to the demo batch-2 bundle.
BATCH_2_PATH = Path("data") / "demo" / "batch-2"


class FailNextNodeBody(BaseModel):
    node: str = "parse_intent"


def _require_test_token(request: Request) -> None:
    """Guard: a matching test-control token must be present in the header."""
    expected = request.app.state.settings.test_control_token
    provided = request.headers.get("X-Test-Control-Token")
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="invalid test-control token")


@router.post("/fail-next-node")
async def fail_next_node(
    request: Request,
    body: FailNextNodeBody = FailNextNodeBody(),
    _token: None = Depends(_require_test_token),
) -> dict[str, Any]:
    """Arm a one-shot node failure for the next graph execution (test-only).

    The instruction is stored in ``app.state`` and surfaced to the graph
    executor via ``run_service.fail_next_node``. It is consumed by the very
    next run and then cleared.
    """
    instruction = {"node": body.node}
    request.app.state.fail_next_node = instruction
    # Surface the instruction to the executor without coupling it to app.state.
    run_service = getattr(request.app.state, "run_service", None)
    if run_service is not None:
        run_service.fail_next_node = body.node
    return {"fail_next_node": True}


@router.post("/import-batch-2")
async def import_batch_2(
    request: Request,
    _token: None = Depends(_require_test_token),
) -> dict[str, Any]:
    """Trigger synthetic-demo Batch 2 import (test-only, bounded)."""
    service = request.app.state.run_service
    importer = SnapshotImporter(
        session_factory=service.session_factory,
        repository_factory=SnapshotRepository,
        object_store=service.object_store,
        clock=service.clock,
    )
    # Resolve the bundle path from the project root (parents[5]:
    # routes -> api -> bidscope -> src -> backend -> root).
    bundle_path = Path(__file__).resolve().parents[5] / BATCH_2_PATH
    record = await importer.import_bundle(bundle_path)
    return {
        "import_batch_2": "ok",
        "bundle_id": record.snapshot_bundle_id,
        "status": record.status,
        "import_id": str(record.id),
    }
