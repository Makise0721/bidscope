from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from bidscope.evaluation.real_contracts import (
    RealEvaluationDatasetManifest,
    RealEvaluationResult,
    validate_real_evaluation_files,
)
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


def test_real_evaluation_files_validate_dataset_linkage_and_hash(tmp_path: Path) -> None:
    manifest_path = tmp_path / "dataset-manifest.json"
    result_path = tmp_path / "result.json"
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

    validated = validate_real_evaluation_files(manifest_path, result_path)

    assert validated.manifest.dataset_id == "ccgp-pilot-eval"
    assert validated.result.run_id == "real-eval-run-20260729-01"


def test_real_evaluation_files_reject_manifest_hash_mismatch(tmp_path: Path) -> None:
    manifest_path = tmp_path / "dataset-manifest.json"
    result_path = tmp_path / "result.json"
    manifest_path.write_text(
        json.dumps(_dataset_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result_path.write_text(
        json.dumps(_evaluation_result(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest hash"):
        validate_real_evaluation_files(manifest_path, result_path)
