"""Loading and validating committed, versioned evaluation JSONL datasets."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[4]
EVAL_ROOT = PROJECT_ROOT / "eval"
CORPUS_PATH = EVAL_ROOT / "corpus" / "synthetic-notices-v1.jsonl"
DATASET_PATHS = {
    "intent-v1": EVAL_ROOT / "data" / "intent-v1.jsonl",
    "retrieval-v1": EVAL_ROOT / "data" / "retrieval-v1.jsonl",
    "dedup-v1": EVAL_ROOT / "data" / "dedup-v1.jsonl",
    "claims-v1": EVAL_ROOT / "data" / "claims-v1.jsonl",
    "e2e-v1": EVAL_ROOT / "data" / "e2e-v1.jsonl",
}
MIN_COUNTS = {
    "intent-v1": 100,
    "retrieval-v1": 30,
    "dedup-v1": 100,
    "claims-v1": 50,
    "e2e-v1": 30,
}
REQUIRED_FIELDS = {
    "corpus": {
        "id", "source", "external_id", "canonical_url", "title", "region",
        "purchaser", "budget_minor_units", "deadline", "project_number",
        "content_hash", "content",
    },
    "intent-v1": {"id", "source", "source_url", "request", "expected", "metadata"},
    "retrieval-v1": {
        "id", "source", "source_url", "query", "filters", "relevant_ids", "expected_top_k",
    },
    "dedup-v1": {
        "id", "source", "source_url", "left", "right", "expected_decision", "metadata",
    },
    "claims-v1": {
        "id", "source", "source_url", "notice_id", "claims", "evidence_ids",
        "expected_supported",
    },
    "e2e-v1": {
        "id", "source", "source_url", "request", "expected_notice_ids", "completed",
        "citations_valid", "expected_items_returned", "latency_ms", "usage",
    },
}


class DatasetError(ValueError):
    """Raised when committed evaluation data is absent, invalid, or tampered."""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DatasetError(f"missing committed dataset: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise DatasetError(f"invalid JSON in {path}:{line_number}: {error}") from error
        if not isinstance(item, dict):
            raise DatasetError(f"dataset record is not an object: {path}:{line_number}")
        records.append(item)
    if not records:
        raise DatasetError(f"empty committed dataset: {path}")
    return records


def _check_id(item: dict[str, Any], path: Path, index: int) -> str:
    record_id = item.get("id")
    if not isinstance(record_id, str) or not record_id.startswith("eval-"):
        raise DatasetError(f"{path}:{index} must have an eval-* id")
    return record_id


def _check_synthetic(item: dict[str, Any], path: Path, index: int) -> None:
    source = item.get("source")
    if source != "synthetic_demo":
        raise DatasetError(f"{path}:{index} must use source=synthetic_demo")
    url = item.get("canonical_url", item.get("source_url"))
    if url is not None:
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or parsed.hostname != "example.invalid":
            raise DatasetError(f"{path}:{index} has a non-reserved synthetic URL")


def _check_nested_synthetic(value: Any, path: Path, index: int) -> None:
    if isinstance(value, dict):
        if "source" in value:
            _check_synthetic(value, path, index)
        for child in value.values():
            _check_nested_synthetic(child, path, index)
    elif isinstance(value, list):
        for child in value:
            _check_nested_synthetic(child, path, index)


def _validate_record_shape(item: dict[str, Any], path: Path, index: int, schema_name: str) -> None:
    """Enforce the small, explicit JSONL schema used by each dataset."""
    checks: dict[str, bool]
    if schema_name == "corpus":
        checks = {
            "source": item.get("source") == "synthetic_demo",
            "title": isinstance(item.get("title"), str),
            "region": isinstance(item.get("region"), str),
            "content": isinstance(item.get("content"), str),
        }
    elif schema_name == "intent-v1":
        checks = {
            "request": isinstance(item.get("request"), str),
            "expected": isinstance(item.get("expected"), dict),
            "metadata": isinstance(item.get("metadata"), dict),
        }
    elif schema_name == "retrieval-v1":
        checks = {
            "query": isinstance(item.get("query"), str),
            "filters": isinstance(item.get("filters"), dict),
            "relevant_ids": isinstance(item.get("relevant_ids"), list),
            "expected_top_k": isinstance(item.get("expected_top_k"), int),
        }
    elif schema_name == "dedup-v1":
        checks = {
            "left": isinstance(item.get("left"), dict),
            "right": isinstance(item.get("right"), dict),
            "expected_decision": item.get("expected_decision") in {
                "exact", "ambiguous", "distinct"
            },
            "metadata": isinstance(item.get("metadata"), dict),
        }
    elif schema_name == "claims-v1":
        checks = {
            "notice_id": isinstance(item.get("notice_id"), str),
            "claims": isinstance(item.get("claims"), list),
            "evidence_ids": isinstance(item.get("evidence_ids"), list),
            "expected_supported": isinstance(item.get("expected_supported"), bool),
        }
    elif schema_name == "e2e-v1":
        usage = item.get("usage")
        checks = {
            "request": isinstance(item.get("request"), str),
            "expected_notice_ids": isinstance(item.get("expected_notice_ids"), list),
            "completed": isinstance(item.get("completed"), bool),
            "citations_valid": isinstance(item.get("citations_valid"), bool),
            "expected_items_returned": isinstance(item.get("expected_items_returned"), bool),
            "latency_ms": isinstance(item.get("latency_ms"), (int, float)),
            "usage": isinstance(usage, dict)
            and isinstance(usage.get("prompt"), int)
            and isinstance(usage.get("completion"), int),
        }
    else:  # pragma: no cover - guarded by the constant schema map
        checks = {}
    invalid = sorted(field for field, valid in checks.items() if not valid)
    if invalid:
        raise DatasetError(f"{path}:{index} has invalid fields: {', '.join(invalid)}")


def _validate_records(
    records: list[dict[str, Any]],
    path: Path,
    *,
    minimum: int | None,
    schema_name: str,
) -> None:
    if minimum is not None and len(records) < minimum:
        raise DatasetError(f"{path} has {len(records)} records; minimum is {minimum}")
    required = REQUIRED_FIELDS[schema_name]
    for index, item in enumerate(records, 1):
        missing = sorted(required - item.keys())
        if missing:
            raise DatasetError(f"{path}:{index} missing required fields: {', '.join(missing)}")
        _validate_record_shape(item, path, index, schema_name)
    ids = [_check_id(item, path, index) for index, item in enumerate(records, 1)]
    duplicates = sorted(record_id for record_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise DatasetError(f"duplicate IDs in {path}: {', '.join(duplicates)}")
    for index, item in enumerate(records, 1):
        if "source" in item or "canonical_url" in item or "source_url" in item:
            _check_synthetic(item, path, index)
        _check_nested_synthetic(item, path, index)


def validate_committed_datasets(
    *, corpus_path: Path = CORPUS_PATH, dataset_paths: dict[str, Path] = DATASET_PATHS
) -> dict[str, list[dict[str, Any]]]:
    """Load all committed JSONL files and enforce their safety/count invariants."""
    corpus = _read_jsonl(corpus_path)
    _validate_records(corpus, corpus_path, minimum=1, schema_name="corpus")
    loaded: dict[str, list[dict[str, Any]]] = {"corpus": corpus}
    for name, path in dataset_paths.items():
        records = _read_jsonl(path)
        _validate_records(records, path, minimum=MIN_COUNTS[name], schema_name=name)
        loaded[name] = records

    all_ids = [item["id"] for records in loaded.values() for item in records]
    duplicates = sorted(record_id for record_id, count in Counter(all_ids).items() if count > 1)
    if duplicates:
        raise DatasetError(f"duplicate IDs across committed datasets: {', '.join(duplicates)}")

    corpus_ids = {item["id"] for item in corpus}
    for item in loaded["retrieval-v1"]:
        relevant = item.get("relevant_ids", [])
        if not isinstance(relevant, list) or not set(relevant).issubset(corpus_ids):
            raise DatasetError(f"retrieval case {item['id']} references unknown corpus ID")
    return loaded


def dataset_hashes(
    *, corpus_path: Path = CORPUS_PATH, dataset_paths: dict[str, Path] = DATASET_PATHS
) -> dict[str, str]:
    """Return SHA-256 hashes of committed files in stable filename order."""
    paths = {"corpus": corpus_path, **dataset_paths}
    return {
        name: hashlib.sha256(paths[name].read_bytes()).hexdigest()
        for name in sorted(paths)
    }


def load_datasets() -> dict[str, list[dict[str, Any]]]:
    """Load the repository's committed evaluation data; never regenerate it."""
    return validate_committed_datasets()


__all__ = [
    "CORPUS_PATH",
    "DATASET_PATHS",
    "DatasetError",
    "EVAL_ROOT",
    "MIN_COUNTS",
    "dataset_hashes",
    "load_datasets",
    "validate_committed_datasets",
]
