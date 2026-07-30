from __future__ import annotations

import hashlib
import json
from base64 import b64encode
from pathlib import Path

import pytest
from bidscope.evaluation.real_contracts import (
    RealEvaluationDatasetManifest,
    RealEvaluationResult,
    validate_real_evaluation_files,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError


def _dataset_manifest() -> dict[str, object]:
    return {
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
        "record_count": 12,
        "created_at": "2026-07-29T09:00:00+00:00",
    }


def _evaluation_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "real-evaluation-result-v1",
        "run_id": "real-eval-run-20260729-01",
        "dataset_id": "ccgp-pilot-eval",
        "dataset_version": "2026-07-29-v1",
        "dataset_manifest_sha256": "0" * 64,
        "snapshot_bundle_ids": ["ccgp-batch-20260729"],
        "mode": "offline_baseline",
        "provider": "offline",
        "model": "fake-deterministic",
        "model_version": "fake-deterministic-v1",
        "prompt_version": "prompt-v1",
        "pricing_snapshot_date": "2026-07-29",
        "environment": "staging",
        "sample_count": 12,
        "failure_policy": "record_and_continue",
        "status": "completed",
        "metrics": {
            "retrieval_recall_at_10": 0.9,
            "retrieval_ndcg_at_10": 0.88,
            "dedup_f1": 0.91,
            "citation_coverage": 1.0,
            "citation_support_accuracy": 0.98,
            "latency_p50_ms": 100.0,
            "latency_p95_ms": 250.0,
            "cost_cny": 0.02,
            "human_usefulness": 0.8,
        },
        "citation_provenance_hard_gate": True,
        "hard_gate_failures": [],
        "failure_codes": [],
    }
    result.update(overrides)
    return result


def _snapshot_catalog() -> dict[str, object]:
    return {
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
    }


def _write_catalog_signature(catalog_path: Path) -> tuple[Path, str]:
    signing_key = Ed25519PrivateKey.generate()
    signature_path = catalog_path.with_suffix(".json.sig")
    signature_path.write_bytes(signing_key.sign(catalog_path.read_bytes()))
    public_key = signing_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return signature_path, b64encode(public_key).decode("ascii")


def test_real_dataset_manifest_rejects_synthetic_source() -> None:
    manifest = _dataset_manifest()
    manifest["source"] = "synthetic_demo"

    with pytest.raises(ValidationError):
        RealEvaluationDatasetManifest.model_validate(manifest)


def test_real_result_rejects_raw_prompt_extra_field() -> None:
    result = _evaluation_result(prompt="must-not-be-persisted")

    with pytest.raises(ValidationError):
        RealEvaluationResult.model_validate(result)


def test_real_result_requires_failure_reason_when_hard_gate_is_false() -> None:
    result = _evaluation_result(
        citation_provenance_hard_gate=False,
        hard_gate_failures=[],
    )

    with pytest.raises(ValidationError):
        RealEvaluationResult.model_validate(result)


def test_failed_real_result_requires_safe_failure_code_and_allows_missing_metrics() -> None:
    result = _evaluation_result(
        status="failed",
        metrics=None,
        failure_codes=["provider_unavailable"],
    )

    validated = RealEvaluationResult.model_validate(result)

    assert validated.metrics is None
    assert validated.failure_codes == ["provider_unavailable"]


def test_real_result_rejects_sensitive_failure_code() -> None:
    result = _evaluation_result(failure_codes=["provider_unavailable\nsecret=token"])

    with pytest.raises(ValidationError):
        RealEvaluationResult.model_validate(result)


def test_real_result_rejects_latency_p95_below_p50() -> None:
    metrics = _evaluation_result()["metrics"]
    assert isinstance(metrics, dict)
    result = _evaluation_result(
        metrics={**metrics, "latency_p50_ms": 250.0, "latency_p95_ms": 100.0}
    )

    with pytest.raises(ValidationError, match="latency_p95_ms"):
        RealEvaluationResult.model_validate(result)


def test_real_evaluation_files_reject_unsigned_snapshot_catalog(tmp_path: Path) -> None:
    manifest_path = tmp_path / "dataset-manifest.json"
    result_path = tmp_path / "result.json"
    catalog_path = tmp_path / "snapshot-admission-catalog.json"
    manifest_path.write_text(json.dumps(_dataset_manifest()) + "\n", encoding="utf-8")
    result_path.write_text(
        json.dumps(
            _evaluation_result(
                dataset_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            )
        )
        + "\n",
        encoding="utf-8",
    )
    catalog_path.write_text(json.dumps(_snapshot_catalog()) + "\n", encoding="utf-8")
    _, public_key = _write_catalog_signature(catalog_path)
    catalog_path.with_suffix(".json.sig").unlink()

    with pytest.raises(ValueError, match="signature"):
        validate_real_evaluation_files(
            manifest_path,
            result_path,
            catalog_path,
            catalog_path.with_suffix(".json.sig"),
            public_key,
        )


def test_real_evaluation_files_rejects_catalog_tampered_after_signing(tmp_path: Path) -> None:
    manifest_path = tmp_path / "dataset-manifest.json"
    result_path = tmp_path / "result.json"
    catalog_path = tmp_path / "snapshot-admission-catalog.json"
    manifest_path.write_text(json.dumps(_dataset_manifest()) + "\n", encoding="utf-8")
    result_path.write_text(
        json.dumps(
            _evaluation_result(
                dataset_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            )
        )
        + "\n",
        encoding="utf-8",
    )
    catalog_path.write_text(json.dumps(_snapshot_catalog()) + "\n", encoding="utf-8")
    signature_path, public_key = _write_catalog_signature(catalog_path)
    catalog_path.write_text('{"tampered": true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="signature"):
        validate_real_evaluation_files(
            manifest_path, result_path, catalog_path, signature_path, public_key
        )


def test_real_evaluation_files_validate_dataset_linkage_and_hash(tmp_path: Path) -> None:
    manifest_path = tmp_path / "dataset-manifest.json"
    result_path = tmp_path / "result.json"
    catalog_path = tmp_path / "snapshot-admission-catalog.json"
    manifest_path.write_text(
        json.dumps(_dataset_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result = _evaluation_result(
        dataset_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    catalog_path.write_text(
        json.dumps(_snapshot_catalog(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    signature_path, public_key = _write_catalog_signature(catalog_path)

    validated = validate_real_evaluation_files(
        manifest_path, result_path, catalog_path, signature_path, public_key
    )

    assert validated.manifest.dataset_id == "ccgp-pilot-eval"
    assert validated.result.run_id == "real-eval-run-20260729-01"
    assert len(validated.snapshot_catalog_sha256) == 64


def test_real_evaluation_files_reject_manifest_hash_mismatch(tmp_path: Path) -> None:
    manifest_path = tmp_path / "dataset-manifest.json"
    result_path = tmp_path / "result.json"
    catalog_path = tmp_path / "snapshot-admission-catalog.json"
    manifest_path.write_text(
        json.dumps(_dataset_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result_path.write_text(
        json.dumps(_evaluation_result(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    catalog_path.write_text(
        json.dumps(_snapshot_catalog(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    signature_path, public_key = _write_catalog_signature(catalog_path)

    with pytest.raises(ValueError, match="manifest hash"):
        validate_real_evaluation_files(
            manifest_path, result_path, catalog_path, signature_path, public_key
        )


def test_real_evaluation_files_reject_unapproved_snapshot_catalog(tmp_path: Path) -> None:
    manifest_path = tmp_path / "dataset-manifest.json"
    result_path = tmp_path / "result.json"
    catalog_path = tmp_path / "snapshot-admission-catalog.json"
    manifest_path.write_text(
        json.dumps(_dataset_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result = _evaluation_result(
        dataset_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    catalog = _snapshot_catalog()
    catalog["snapshots"][0]["review_status"] = "pending"  # type: ignore[index]
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    signature_path, public_key = _write_catalog_signature(catalog_path)

    with pytest.raises(ValueError, match="approved"):
        validate_real_evaluation_files(
            manifest_path, result_path, catalog_path, signature_path, public_key
        )
