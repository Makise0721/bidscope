from __future__ import annotations

import hashlib
import json
from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace

from bidscope import cli
from bidscope.snapshots.adapters import InspectionError, InspectionResult
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner


def _write_catalog_signature(catalog_path: Path) -> tuple[Path, str]:
    signing_key = Ed25519PrivateKey.generate()
    signature_path = catalog_path.with_suffix(".json.sig")
    signature_path.write_bytes(signing_key.sign(catalog_path.read_bytes()))
    public_key = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return signature_path, b64encode(public_key).decode("ascii")


def test_snapshot_inspect_json_exposes_quarantine_disposition(tmp_path: Path, monkeypatch) -> None:
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


def test_snapshot_import_json_exposes_auditable_metrics(tmp_path: Path, monkeypatch) -> None:
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
    assert payload["reprocessing"] == "new"


def test_validate_real_evaluation_cli_is_separate_from_deterministic_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    result_path = tmp_path / "result.json"
    catalog_path = tmp_path / "catalog.json"
    manifest = {
        "schema_version": "real-evaluation-dataset-v1",
        "dataset_id": "ccgp-pilot-eval",
        "dataset_version": "2026-07-29-v1",
        "source": "ccgp",
        "capture_kind": "curated_public_excerpt",
        "snapshot_bundle_ids": ["ccgp-batch-20260729"],
        "snapshot_hashes": {"ccgp-batch-20260729": "a" * 64},
        "annotation_guide_version": "guide-v1",
        "annotation_set_version": "labels-v1",
        "access_class": "restricted_staging",
        "record_count": 1,
        "created_at": "2026-07-29T09:00:00+00:00",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": "snapshot-admission-catalog-v1",
                "snapshots": [
                    {
                        "bundle_id": "ccgp-batch-20260729",
                        "bundle_hash": "a" * 64,
                        "batch_id": "ccgp-batch-20260729",
                        "source": "ccgp",
                        "capture_kind": "curated_public_excerpt",
                        "schema_version": 2,
                        "review_status": "approved",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    signature_path, public_key = _write_catalog_signature(catalog_path)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(real_evaluation_catalog_public_key=public_key),
    )
    result = {
        "schema_version": "real-evaluation-result-v1",
        "run_id": "real-eval-run-20260729-01",
        "dataset_id": "ccgp-pilot-eval",
        "dataset_version": "2026-07-29-v1",
        "dataset_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "snapshot_bundle_ids": ["ccgp-batch-20260729"],
        "mode": "offline_baseline",
        "provider": "offline",
        "model": "fake-deterministic",
        "model_version": "fake-deterministic-v1",
        "prompt_version": "prompt-v1",
        "pricing_snapshot_date": "2026-07-29",
        "environment": "staging",
        "sample_count": 1,
        "failure_policy": "record_and_continue",
        "status": "completed",
        "metrics": {
            "retrieval_recall_at_10": 1,
            "retrieval_ndcg_at_10": 1,
            "dedup_f1": 1,
            "citation_coverage": 1,
            "citation_support_accuracy": 1,
            "latency_p50_ms": 1,
            "latency_p95_ms": 1,
            "cost_cny": 0,
            "human_usefulness": 1,
        },
        "citation_provenance_hard_gate": True,
        "hard_gate_failures": [],
        "failure_codes": [],
    }
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        [
            "eval",
            "validate-real",
            "--manifest",
            str(manifest_path),
            "--result",
            str(result_path),
            "--catalog",
            str(catalog_path),
            "--catalog-signature",
            str(signature_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "validated"
    assert payload["release_decision"] == "review_required"
    assert payload["deterministic_target_pass"] is None


def test_validate_real_evaluation_cli_blocks_failed_result(tmp_path: Path, monkeypatch) -> None:
    manifest_path = tmp_path / "manifest.json"
    result_path = tmp_path / "result.json"
    catalog_path = tmp_path / "catalog.json"
    manifest_path.write_text("{}", encoding="utf-8")
    result_path.write_text("{}", encoding="utf-8")
    catalog_path.write_text("{}", encoding="utf-8")
    signature_path = tmp_path / "catalog.json.sig"
    signature_path.write_bytes(b"signature")

    def fake_validate_real_evaluation_files(
        _manifest_path: Path,
        _result_path: Path,
        _catalog_path: Path,
        _signature_path: Path,
        _public_key: str | None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            manifest=SimpleNamespace(
                dataset_id="ccgp-pilot-eval",
                dataset_version="2026-07-29-v1",
            ),
            result=SimpleNamespace(
                run_id="real-eval-run-20260729-02",
                mode="staging_live_model",
                status="failed",
                citation_provenance_hard_gate=True,
                metrics=SimpleNamespace(model_dump=lambda mode: {}),
                hard_gate_failures=[],
                failure_codes=["provider_unavailable"],
            ),
            manifest_sha256="a" * 64,
            snapshot_catalog_sha256="b" * 64,
        )

    monkeypatch.setattr(cli, "validate_real_evaluation_files", fake_validate_real_evaluation_files)

    result = CliRunner().invoke(
        cli.app,
        [
            "eval",
            "validate-real",
            "--manifest",
            str(manifest_path),
            "--result",
            str(result_path),
            "--catalog",
            str(catalog_path),
            "--catalog-signature",
            str(signature_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["release_decision"] == "blocked"
    assert payload["failure_codes"] == ["provider_unavailable"]
    assert payload["deterministic_target_pass"] is None


def test_validate_real_evaluation_cli_fails_closed_without_leaking_key_material(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    result_path = tmp_path / "result.json"
    catalog_path = tmp_path / "catalog.json"
    signature_path = tmp_path / "catalog.sig"
    key_material = "sensitive-public-key-material"
    manifest_path.write_text("{}", encoding="utf-8")
    result_path.write_text("{}", encoding="utf-8")
    catalog_path.write_text("{}", encoding="utf-8")
    signature_path.write_bytes(b"not-a-valid-signature")
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(real_evaluation_catalog_public_key=key_material),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "eval",
            "validate-real",
            "--manifest",
            str(manifest_path),
            "--result",
            str(result_path),
            "--catalog",
            str(catalog_path),
            "--catalog-signature",
            str(signature_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid"
    assert payload["release_decision"] == "blocked"
    assert key_material not in result.stdout
    assert str(signature_path) not in result.stdout
