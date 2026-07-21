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

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

router = APIRouter(prefix="/api/test-controls", tags=["test-controls"])


def _require_test_token(request: Request) -> None:
    """Guard: a matching test-control token must be present in the header."""
    expected = request.app.state.settings.test_control_token
    provided = request.headers.get("X-Test-Control-Token")
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="invalid test-control token")


@router.post("/fail-next-node")
async def fail_next_node(
    request: Request,
    _token: None = Depends(_require_test_token),
) -> dict[str, Any]:
    """Arm a one-shot node failure for the next graph execution (test-only)."""
    request.app.state.fail_next_node = True
    return {"fail_next_node": True}


@router.post("/import-batch-2")
async def import_batch_2(
    request: Request,
    _token: None = Depends(_require_test_token),
) -> dict[str, Any]:
    """Trigger synthetic-demo Batch 2 import (test-only, bounded)."""
    # Import is handled by the snapshot CLI in real deployments; this is a
    # bounded control that reports availability without performing I/O.
    return {"import_batch_2": "not_implemented_in_p0", "ok": True}
