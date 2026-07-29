from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from bidscope import cli
from bidscope.snapshots.adapters import InspectionError, InspectionResult
from typer.testing import CliRunner


def test_snapshot_inspect_json_exposes_quarantine_disposition(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    inspection = InspectionResult(
        valid=False,
        bundle_id="ccgp-batch-20260729",
        errors=[InspectionError("authorization_not_approved", "review required")],
        disposition="quarantined",
    )
    monkeypatch.setattr(
        cli,
        "_build_importer",
        lambda: SimpleNamespace(import_inspect=lambda _: inspection),
    )

    result = CliRunner().invoke(cli.app, ["snapshots", "inspect", str(bundle), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid"
    assert payload["disposition"] == "quarantined"


def test_snapshot_import_json_exposes_auditable_metrics(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    record = SimpleNamespace(
        snapshot_bundle_id="bundle-db-id",
        status="success",
        id="import-db-id",
        metrics={"manifest_sha256": "a" * 64, "notice_count": 1},
        warnings={"parser": []},
    )

    async def fake_run_import(_: Path) -> object:
        return record

    monkeypatch.setattr(cli, "_run_import", fake_run_import)

    result = CliRunner().invoke(cli.app, ["snapshots", "import", str(bundle), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["bundle_hash"] == "a" * 64
    assert payload["metrics"] == {"manifest_sha256": "a" * 64, "notice_count": 1}
    assert payload["warnings"] == {"parser": []}
