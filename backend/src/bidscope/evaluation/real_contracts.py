"""Versioned contracts for restricted real-data evaluation artifacts.

These contracts validate metadata and measured summaries only. They deliberately
do not execute a model, read prompts, or access a source URL.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bidscope.domain.types import AwareDatetime

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_REAL_EVALUATION_RECORDS = 1_000_000

BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=128)]
BoundedLabel = Annotated[str, Field(min_length=1, max_length=128)]
BoundedCode = Annotated[str, Field(min_length=1, max_length=64)]


class RealEvaluationContractError(ValueError):
    """Raised when two restricted evaluation artifacts cannot be linked safely."""


class RealEvaluationDatasetManifest(BaseModel):
    """Access-controlled identity and provenance for one real evaluation set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real-evaluation-dataset-v1"]
    dataset_id: BoundedIdentifier
    dataset_version: BoundedLabel
    source: Literal["ccgp"]
    capture_kind: Literal["curated_public_excerpt"]
    snapshot_bundle_ids: Annotated[list[BoundedIdentifier], Field(min_length=1, max_length=100)]
    snapshot_hashes: Annotated[dict[str, str], Field(min_length=1, max_length=100)]
    annotation_guide_version: BoundedLabel
    annotation_set_version: BoundedLabel
    access_class: Literal["restricted_staging"]
    record_count: Annotated[int, Field(gt=0, le=_MAX_REAL_EVALUATION_RECORDS)]
    created_at: AwareDatetime

    @field_validator(
        "dataset_id",
        "dataset_version",
        "annotation_guide_version",
        "annotation_set_version",
    )
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("evaluation identifiers must be bounded path-safe labels")
        return value

    @field_validator("snapshot_bundle_ids")
    @classmethod
    def _validate_snapshot_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not _IDENTIFIER_RE.fullmatch(item) for item in value
        ):
            raise ValueError("snapshot_bundle_ids must be unique path-safe identifiers")
        return value

    @field_validator("snapshot_hashes")
    @classmethod
    def _validate_snapshot_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not _IDENTIFIER_RE.fullmatch(key) for key in value):
            raise ValueError("snapshot_hashes keys must be path-safe identifiers")
        if any(not _SHA256_RE.fullmatch(item) for item in value.values()):
            raise ValueError("snapshot_hashes values must be SHA-256 hashes")
        return value

    @model_validator(mode="after")
    def _validate_snapshot_linkage(self) -> RealEvaluationDatasetManifest:
        if set(self.snapshot_bundle_ids) != set(self.snapshot_hashes):
            raise ValueError("snapshot_bundle_ids and snapshot_hashes must contain the same IDs")
        return self


class RealEvaluationMetrics(BaseModel):
    """Bounded measurements that are comparable without exposing source data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_recall_at_10: float = Field(ge=0, le=1)
    retrieval_ndcg_at_10: float = Field(ge=0, le=1)
    dedup_f1: float = Field(ge=0, le=1)
    citation_coverage: float = Field(ge=0, le=1)
    citation_support_accuracy: float = Field(ge=0, le=1)
    latency_p50_ms: float = Field(ge=0)
    latency_p95_ms: float = Field(ge=0)
    cost_cny: float = Field(ge=0)
    human_usefulness: float | None = Field(default=None, ge=0, le=1)


class RealEvaluationResult(BaseModel):
    """A measured real-data run, separate from deterministic CI evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real-evaluation-result-v1"]
    run_id: BoundedIdentifier
    dataset_id: BoundedIdentifier
    dataset_version: BoundedLabel
    dataset_manifest_sha256: str
    snapshot_bundle_ids: Annotated[list[BoundedIdentifier], Field(min_length=1, max_length=100)]
    mode: Literal["offline_baseline", "staging_live_model"]
    provider: BoundedLabel
    model: BoundedLabel
    model_version: BoundedLabel
    prompt_version: BoundedLabel
    pricing_snapshot_date: date
    environment: Literal["staging"]
    sample_count: Annotated[int, Field(gt=0, le=_MAX_REAL_EVALUATION_RECORDS)]
    failure_policy: Literal["fail_closed", "record_and_continue"]
    status: Literal["completed", "failed"]
    metrics: RealEvaluationMetrics
    citation_provenance_hard_gate: bool
    hard_gate_failures: Annotated[list[BoundedCode], Field(max_length=32)]
    failure_codes: Annotated[list[BoundedCode], Field(max_length=32)]

    @field_validator(
        "run_id",
        "dataset_id",
        "dataset_version",
        "provider",
        "model",
        "model_version",
        "prompt_version",
    )
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("evaluation metadata must be bounded path-safe labels")
        return value

    @field_validator("dataset_manifest_sha256")
    @classmethod
    def _validate_dataset_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("dataset_manifest_sha256 must be a SHA-256 hash")
        return value

    @field_validator("snapshot_bundle_ids")
    @classmethod
    def _validate_snapshot_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not _IDENTIFIER_RE.fullmatch(item) for item in value
        ):
            raise ValueError("snapshot_bundle_ids must be unique path-safe identifiers")
        return value

    @model_validator(mode="after")
    def _validate_hard_gate(self) -> RealEvaluationResult:
        if self.citation_provenance_hard_gate and self.hard_gate_failures:
            raise ValueError("hard_gate_failures must be empty when the hard gate passes")
        if not self.citation_provenance_hard_gate and not self.hard_gate_failures:
            raise ValueError("failed citation/provenance hard gate requires failure codes")
        return self


class ValidatedRealEvaluation(BaseModel):
    """Validated pair returned by the staging acceptance command."""

    model_config = ConfigDict(frozen=True)

    manifest: RealEvaluationDatasetManifest
    result: RealEvaluationResult
    manifest_sha256: str


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RealEvaluationContractError(f"unable to read evaluation artifact: {path}") from error
    if not isinstance(value, dict):
        raise RealEvaluationContractError(f"evaluation artifact must be a JSON object: {path}")
    return value


def validate_real_evaluation_files(
    manifest_path: Path, result_path: Path
) -> ValidatedRealEvaluation:
    """Validate restricted evaluation metadata and its dataset linkage."""
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = RealEvaluationDatasetManifest.model_validate(_load_json(manifest_path))
    result = RealEvaluationResult.model_validate(_load_json(result_path))

    if result.dataset_manifest_sha256 != manifest_hash:
        raise RealEvaluationContractError(
            "evaluation result manifest hash does not match manifest hash"
        )
    if result.dataset_id != manifest.dataset_id:
        raise RealEvaluationContractError("evaluation result dataset ID does not match manifest")
    if result.dataset_version != manifest.dataset_version:
        raise RealEvaluationContractError(
            "evaluation result dataset version does not match manifest"
        )
    if set(result.snapshot_bundle_ids) != set(manifest.snapshot_bundle_ids):
        raise RealEvaluationContractError("evaluation result snapshot IDs do not match manifest")
    if result.sample_count > manifest.record_count:
        raise RealEvaluationContractError(
            "evaluation result sample count exceeds dataset record count"
        )

    return ValidatedRealEvaluation(
        manifest=manifest,
        result=result,
        manifest_sha256=manifest_hash,
    )


__all__ = [
    "RealEvaluationContractError",
    "RealEvaluationDatasetManifest",
    "RealEvaluationMetrics",
    "RealEvaluationResult",
    "ValidatedRealEvaluation",
    "validate_real_evaluation_files",
]
