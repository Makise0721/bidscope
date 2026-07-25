"""BidScope command-line interface.

Exposes administrative commands for the snapshot ingestion plane and the
durable execution layer:

* ``bidscope snapshots inspect <bundle>`` — run integrity inspection only.
* ``bidscope snapshots import <bundle>`` — import a verified bundle.
* ``bidscope checkpoints setup`` — create the LangGraph checkpoint tables.

The first two honour ``--json`` for machine-readable output and never access
the network: all work runs against local files and the configured database.
``checkpoints setup`` is the only path that creates the checkpoint schema; the
executor never calls it implicitly.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from bidscope.clock import SystemClock
from bidscope.config import get_settings
from bidscope.db import create_engine_and_session
from bidscope.delivery.objects import LocalObjectStore
from bidscope.evaluation.datasets import DatasetError
from bidscope.evaluation.runner import EvaluationExecutionError, run_deterministic
from bidscope.graph.executor import run_setup_checkpoints
from bidscope.persistence.repositories import SnapshotRepository
from bidscope.snapshots.importer import SnapshotImporter, SnapshotImportError

app = typer.Typer(
    add_completion=False,
    help="BidScope administrative commands.",
    no_args_is_help=True,
)

snapshots_app = typer.Typer(
    help="Snapshot ingestion commands.",
    no_args_is_help=True,
)
app.add_typer(snapshots_app, name="snapshots")

checkpoints_app = typer.Typer(
    help="LangGraph checkpoint administration.",
    no_args_is_help=True,
)
app.add_typer(checkpoints_app, name="checkpoints")

scheduler_app = typer.Typer(
    help="Subscription scheduler process role.",
    no_args_is_help=True,
)
app.add_typer(scheduler_app, name="scheduler")

eval_app = typer.Typer(
    help="Offline evaluation commands.",
    no_args_is_help=True,
)
app.add_typer(eval_app, name="eval")

api_app = typer.Typer(
    help="Run the BidScope API server.",
    no_args_is_help=True,
)
app.add_typer(api_app, name="api")


def configure_windows_selector_event_loop_policy() -> None:
    """Use selector-backed loops for psycopg async connections on Windows."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _build_importer() -> SnapshotImporter:
    """Construct an importer backed by the configured database and object store."""
    _, session_factory = create_engine_and_session()
    return SnapshotImporter(
        session_factory=session_factory,
        repository_factory=SnapshotRepository,
        object_store=LocalObjectStore(".data/objects"),
        clock=SystemClock(),
    )


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


async def _run_import(bundle: Path) -> Any:
    """Run the async import and return the ``SnapshotImport`` record."""
    return await _build_importer().import_bundle(bundle)


# --- snapshots inspect -------------------------------------------------------


@snapshots_app.command("inspect")
def snapshots_inspect(
    bundle: Annotated[Path, typer.Argument(exists=True, dir_okay=True, resolve_path=True)],
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON output.")] = False,
) -> None:
    """Inspect a snapshot bundle's integrity without importing it."""
    importer = _build_importer()
    try:
        inspection = importer.import_inspect(bundle)
    except SnapshotImportError as error:
        payload = {
            "valid": False,
            "bundle_id": None,
            "status": "invalid",
            "errors": [str(error)],
        }
        if json_output:
            typer.echo(_json_payload(payload))
        else:
            typer.echo(f"invalid: {error}", err=True)
        raise typer.Exit(code=1) from None

    result: dict[str, Any] = {
        "valid": inspection.valid,
        "bundle_id": inspection.bundle_id,
        "status": "valid" if inspection.valid else "invalid",
        "errors": [
            {"code": e.code, "message": e.message, "path": e.path}
            for e in inspection.errors
        ],
    }
    if json_output:
        typer.echo(_json_payload(result))
    else:
        if inspection.valid:
            typer.echo(f"valid bundle: {inspection.bundle_id}")
        else:
            for e in inspection.errors:
                typer.echo(f"  {e.code}: {e.message}", err=True)
            raise typer.Exit(code=1) from None


# --- snapshots import --------------------------------------------------------


@snapshots_app.command("import")
def snapshots_import(
    bundle: Annotated[Path, typer.Argument(exists=True, dir_okay=True, resolve_path=True)],
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON output.")] = False,
) -> None:
    """Import a verified snapshot bundle idempotently."""
    configure_windows_selector_event_loop_policy()
    try:
        record = asyncio.run(_run_import(bundle))
    except SnapshotImportError as error:
        payload = {
            "valid": False,
            "bundle_id": None,
            "status": "failed",
            "errors": [str(error)],
        }
        if json_output:
            typer.echo(_json_payload(payload))
        else:
            typer.echo(f"import failed: {error}", err=True)
        raise typer.Exit(code=1) from None

    result: dict[str, Any] = {
        "valid": True,
        "bundle_id": record.snapshot_bundle_id,
        "status": record.status,
        "import_id": str(record.id),
        "errors": [],
    }
    if json_output:
        typer.echo(_json_payload(result))
    else:
        typer.echo(f"imported ({record.status}): {record.snapshot_bundle_id}")


# --- checkpoints setup -------------------------------------------------------


@checkpoints_app.command("setup")
def checkpoints_setup() -> None:
    """Create the LangGraph checkpoint tables in the configured database."""
    run_setup_checkpoints(get_settings())
    typer.echo("checkpoint tables ready")


# --- evaluation --------------------------------------------------------------


@eval_app.command("run")
def evaluation_run(
    mode: Annotated[str, typer.Option("--mode", help="Evaluation mode.")] = "deterministic",
    output: Annotated[Path, typer.Option("--output", help="Machine-readable result path.")] = Path(
        "eval/results/deterministic.json"
    ),
) -> None:
    """Run an offline evaluation over committed versioned datasets."""
    if mode != "deterministic":
        typer.echo(f"unsupported evaluation mode: {mode}", err=True)
        raise typer.Exit(code=1)
    try:
        result = run_deterministic(output=output)
    except (DatasetError, EvaluationExecutionError, OSError) as error:
        typer.echo(f"evaluation failed: {error}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(_json_payload(result))


# --- api ---------------------------------------------------------------------


@api_app.command("serve", help="Start the BidScope API server.")
def api_serve(
    host: Annotated[str, typer.Option("--host")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port")] = 8000,
) -> None:
    """Serve the FastAPI app (SPA + API) via uvicorn."""
    import uvicorn

    from bidscope.main import app

    configure_windows_selector_event_loop_policy()
    uvicorn.run(app, host=host, port=port)


# --- scheduler ---------------------------------------------------------------


@scheduler_app.command("run")
def scheduler_run_once() -> None:
    """Run one scheduler tick immediately (used by tests and manual triggers)."""
    from bidscope.subscriptions.scheduler import run_scheduler_tick

    configure_windows_selector_event_loop_policy()
    counters = asyncio.run(run_scheduler_tick(get_settings()))
    typer.echo(
        "scheduler tick: "
        f"due={counters['due']} "
        f"ran={counters['ran']} "
        f"skipped={counters['skipped']} "
        f"failed={counters['failed']}"
    )


@scheduler_app.command("start")
def scheduler_start() -> None:
    """Start the APScheduler process role (blocks; one instance per host)."""
    from bidscope.subscriptions.scheduler import start_scheduler

    configure_windows_selector_event_loop_policy()
    scheduler = start_scheduler()
    typer.echo("subscription scheduler started (Ctrl+C to stop)")
    try:
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.shutdown()
        typer.echo("scheduler stopped")


if __name__ == "__main__":
    app()
