"""Loading and validating committed, versioned evaluation JSONL datasets."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class DatasetError(ValueError):
    """Raised when committed evaluation data is absent, invalid, or tampered."""


def _find_repository_root() -> Path:
    """Find the source checkout containing this package and the eval artifacts."""
    package_path = Path(__file__).resolve()
    for candidate in (package_path.parent, *package_path.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "eval").is_dir():
            return candidate
    raise DatasetError(
        "BidScope evaluation datasets require a source checkout containing pyproject.toml and eval/"
    )


PROJECT_ROOT = _find_repository_root()
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

# These are the approved canonical bytes for the committed synthetic fixture.
# A changed artifact must be deliberately approved by changing this manifest.
EXPECTED_DATASET_HASHES = {
    "claims-v1": "6330bc0d8571a1c27ed5d2630d05f0885fcef5d7ac0273693f129467f226c2e6",
    "corpus": "1a5c3ce1b79bbc948af16bb3cfcfa08603c2d384c4f1a516a792bf72b9657144",
    "dedup-v1": "9863385c8aa33fdc26660d9623864170f34998e2d6cd96f926814e7785f5bb50",
    "e2e-v1": "9eeb9e50f92609509ad94359e3123358ca402837c906ca90079cdc7feb431a2c",
    "intent-v1": "1bf278ce9dbce2ecef001c291c2cf91f606518fe4650c3c3de60829498a7cf2e",
    "retrieval-v1": "ca791e0769c5ac33d4ab24f53349273597b8f296f4d517cb7090c686ad901d8c",
}


# Fields used by NoticeView. Project numbers may be absent for intentionally
# ambiguous pairs; all other values remain scalar in the JSONL contract.
_DEDUP_SCALAR_FIELDS: dict[str, tuple[type[Any], ...]] = {
    "source": (str,),
    "external_id": (str,),
    "canonical_url": (str,),
    "project_number": (str, type(None)),
    "content_hash": (str,),
    "title": (str, type(None)),
    "purchaser": (str, type(None)),
    "region": (str, type(None)),
    "budget_minor_units": (int, float, type(None)),
    "budget_currency": (str, type(None)),
    "deadline": (str, type(None)),
    "procurement_scope": (str, type(None)),
    "cancellation": (bool,),
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DatasetError(f"missing committed dataset: {path}")
    try:
        payload = path.read_bytes()
        text = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise DatasetError(f"unable to read dataset {path}: {error}") from error
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.split("\n"), 1):
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
        parsed = urlparse(url)
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


def _is_finite_number(value: Any, *, nonnegative: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(float(value)):
        return False
    return not nonnegative or value >= 0


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_dedup_notice(value: Any, path: Path, index: int, side: str) -> None:
    if not isinstance(value, dict):
        raise DatasetError(f"{path}:{index} {side} must be an object")
    missing = sorted(set(_DEDUP_SCALAR_FIELDS) - value.keys())
    if missing:
        raise DatasetError(f"{path}:{index} {side} missing required fields: {', '.join(missing)}")
    invalid = [
        field
        for field, expected_types in _DEDUP_SCALAR_FIELDS.items()
        if not isinstance(value.get(field), expected_types)
    ]
    if invalid:
        raise DatasetError(f"{path}:{index} {side} has invalid fields: {', '.join(invalid)}")
    if not _is_finite_number(value["budget_minor_units"], nonnegative=True):
        raise DatasetError(
            f"{path}:{index} {side}.budget_minor_units must be nonnegative finite numeric"
        )
    supporting = value.get("claim_supporting_texts")
    if not isinstance(supporting, (list, tuple)) or not all(
        isinstance(item, str) for item in supporting
    ):
        raise DatasetError(f"{path}:{index} {side}.claim_supporting_texts must be list[str]")


def _validate_record_shape(item: dict[str, Any], path: Path, index: int, schema_name: str) -> None:
    """Enforce the explicit, nested JSONL schema used by each dataset."""
    checks: dict[str, bool]
    if schema_name == "corpus":
        checks = {
            "source": item.get("source") == "synthetic_demo",
            "external_id": isinstance(item.get("external_id"), str),
            "canonical_url": isinstance(item.get("canonical_url"), str),
            "title": isinstance(item.get("title"), str),
            "region": isinstance(item.get("region"), str),
            "purchaser": isinstance(item.get("purchaser"), str),
            "budget_minor_units": _is_finite_number(
                item.get("budget_minor_units"), nonnegative=True
            ),
            "deadline": isinstance(item.get("deadline"), str),
            "project_number": isinstance(item.get("project_number"), str),
            "content_hash": isinstance(item.get("content_hash"), str),
            "content": isinstance(item.get("content"), str),
        }
    elif schema_name == "intent-v1":
        expected = item.get("expected")
        checks = {
            "source_url": isinstance(item.get("source_url"), str),
            "request": isinstance(item.get("request"), str),
            "expected": isinstance(expected, dict),
            "metadata": isinstance(item.get("metadata"), dict),
        }
        if isinstance(expected, dict) and "error" not in expected:
            checks.update({
                "expected.topics": _string_list(expected.get("topics")),
                "expected.expanded_terms": _string_list(expected.get("expanded_terms")),
                "expected.regions": _string_list(expected.get("regions")),
            })
    elif schema_name == "retrieval-v1":
        filters = item.get("filters")
        checks = {
            "source_url": isinstance(item.get("source_url"), str),
            "query": isinstance(item.get("query"), str),
            "filters": isinstance(filters, dict),
            "relevant_ids": _string_list(item.get("relevant_ids")),
            "expected_top_k": isinstance(item.get("expected_top_k"), int)
            and not isinstance(item.get("expected_top_k"), bool)
            and item.get("expected_top_k", 0) > 0,
        }
        if isinstance(filters, dict):
            checks["filters.regions"] = _string_list(filters.get("regions"))
    elif schema_name == "dedup-v1":
        _validate_dedup_notice(item.get("left"), path, index, "left")
        _validate_dedup_notice(item.get("right"), path, index, "right")
        checks = {
            "source_url": isinstance(item.get("source_url"), str),
            "expected_decision": item.get("expected_decision")
            in {"exact", "ambiguous", "distinct"},
            "metadata": isinstance(item.get("metadata"), dict),
        }
    elif schema_name == "claims-v1":
        claims = item.get("claims")
        checks = {
            "source_url": isinstance(item.get("source_url"), str),
            "notice_id": isinstance(item.get("notice_id"), str),
            "claims": isinstance(claims, list),
            "evidence_ids": _string_list(item.get("evidence_ids")),
            "expected_supported": isinstance(item.get("expected_supported"), bool),
        }
        if isinstance(claims, list):
            checks["claims.items"] = all(
                isinstance(claim, dict)
                and isinstance(claim.get("text"), str)
                and _string_list(claim.get("citation_ids"))
                for claim in claims
            )
    elif schema_name == "e2e-v1":
        usage = item.get("usage")
        checks = {
            "source_url": isinstance(item.get("source_url"), str),
            "request": isinstance(item.get("request"), str),
            "expected_notice_ids": _string_list(item.get("expected_notice_ids")),
            "completed": isinstance(item.get("completed"), bool),
            "citations_valid": isinstance(item.get("citations_valid"), bool),
            "expected_items_returned": isinstance(item.get("expected_items_returned"), bool),
            "latency_ms": _is_finite_number(item.get("latency_ms"), nonnegative=True),
            "usage": isinstance(usage, dict)
            and _is_nonnegative_int(usage.get("prompt"))
            and _is_nonnegative_int(usage.get("completion")),
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


def _validate_bundle(
    loaded: dict[str, list[dict[str, Any]]], *, minimums: bool
) -> None:
    corpus = loaded["corpus"]
    _validate_records(corpus, Path("corpus"), minimum=1 if minimums else None, schema_name="corpus")
    for name, records in loaded.items():
        if name == "corpus":
            continue
        _validate_records(
            records,
            Path(name),
            minimum=MIN_COUNTS[name] if minimums else None,
            schema_name=name,
        )

    all_ids = [item["id"] for records in loaded.values() for item in records]
    duplicates = sorted(record_id for record_id, count in Counter(all_ids).items() if count > 1)
    if duplicates:
        raise DatasetError(f"duplicate IDs across committed datasets: {', '.join(duplicates)}")

    corpus_ids = {item["id"] for item in corpus}
    for item in loaded["retrieval-v1"]:
        if not set(item["relevant_ids"]).issubset(corpus_ids):
            raise DatasetError(f"retrieval case {item['id']} references unknown corpus ID")


def validate_generated_bundle(
    corpus: list[dict[str, Any]], datasets: dict[str, list[dict[str, Any]]]
) -> None:
    """Validate an in-memory bundle before a builder writes any artifact."""
    if set(datasets) != set(DATASET_PATHS):
        raise DatasetError("generated datasets do not match the committed dataset manifest")
    _validate_bundle({"corpus": corpus, **datasets}, minimums=True)


def _canonical_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise DatasetError(f"missing committed dataset: {path}")
    try:
        return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    except OSError as error:
        raise DatasetError(f"unable to hash dataset {path}: {error}") from error


def _is_default_paths(corpus_path: Path, dataset_paths: dict[str, Path]) -> bool:
    return corpus_path == CORPUS_PATH and dataset_paths == DATASET_PATHS


def _verify_expected_hashes(hashes: dict[str, str]) -> None:
    if hashes != EXPECTED_DATASET_HASHES:
        raise DatasetError(
            "committed evaluation dataset hash does not match the approved manifest"
        )


def validate_committed_datasets(
    *, corpus_path: Path = CORPUS_PATH, dataset_paths: dict[str, Path] = DATASET_PATHS
) -> dict[str, list[dict[str, Any]]]:
    """Load all committed JSONL files and enforce safety, count, and hash invariants."""
    corpus = _read_jsonl(corpus_path)
    loaded: dict[str, list[dict[str, Any]]] = {"corpus": corpus}
    for name, path in dataset_paths.items():
        records = _read_jsonl(path)
        loaded[name] = records
    if set(dataset_paths) != set(DATASET_PATHS):
        raise DatasetError("dataset paths do not match the committed dataset manifest")
    _validate_bundle(loaded, minimums=True)
    if _is_default_paths(corpus_path, dataset_paths):
        _verify_expected_hashes(
            dataset_hashes(corpus_path=corpus_path, dataset_paths=dataset_paths)
        )
    return loaded


def dataset_hashes(
    *, corpus_path: Path = CORPUS_PATH, dataset_paths: dict[str, Path] = DATASET_PATHS
) -> dict[str, str]:
    """Return SHA-256 hashes of canonical LF-normalized files in filename order."""
    paths = {"corpus": corpus_path, **dataset_paths}
    return {
        name: hashlib.sha256(_canonical_bytes(paths[name])).hexdigest()
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
    "EXPECTED_DATASET_HASHES",
    "MIN_COUNTS",
    "PROJECT_ROOT",
    "dataset_hashes",
    "load_datasets",
    "validate_committed_datasets",
    "validate_generated_bundle",
]
