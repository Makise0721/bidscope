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
from pathlib import Path
from typing import Annotated, Any

import typer

from bidscope.clock import SystemClock
from bidscope.config import get_settings
from bidscope.db import create_engine_and_session
from bidscope.delivery.objects import LocalObjectStore
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


if __name__ == "__main__":
    app()
