"""Loading and validating committed, versioned evaluation JSONL datasets."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class DatasetError(ValueError):
    """Raised when committed evaluation data is absent, invalid, or tampered."""

    _MAX_MESSAGE_LENGTH = 512

    def __init__(self, message: str) -> None:
        bounded = str(message)
        if len(bounded) > self._MAX_MESSAGE_LENGTH:
            bounded = bounded[: self._MAX_MESSAGE_LENGTH - 3] + "..."
        super().__init__(bounded)


def _find_repository_root() -> Path | None:
    """Find a source checkout containing the un-packaged eval artifacts, if any."""
    package_path = Path(__file__).resolve()
    for candidate in (package_path.parent, *package_path.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "eval").is_dir():
            return candidate
    return None


PathLike = Path | Traversable

PROJECT_ROOT = _find_repository_root()
EVAL_ROOT = PROJECT_ROOT / "eval" if PROJECT_ROOT is not None else None
_PACKAGE_ROOT = resources.files("bidscope.evaluation")
_PACKAGE_CORPUS_PATH: PathLike = _PACKAGE_ROOT / "corpus" / "synthetic-notices-v1.jsonl"
_PACKAGE_DATASET_PATHS: dict[str, PathLike] = {
    "intent-v1": _PACKAGE_ROOT / "data" / "intent-v1.jsonl",
    "retrieval-v1": _PACKAGE_ROOT / "data" / "retrieval-v1.jsonl",
    "dedup-v1": _PACKAGE_ROOT / "data" / "dedup-v1.jsonl",
    "claims-v1": _PACKAGE_ROOT / "data" / "claims-v1.jsonl",
    "e2e-v1": _PACKAGE_ROOT / "data" / "e2e-v1.jsonl",
}
# Public paths point at the checkout when it exists, preserving builder and test
# APIs. Installed wheels use the package-resource paths selected by load_datasets.
CORPUS_PATH: PathLike = (
    EVAL_ROOT / "corpus" / "synthetic-notices-v1.jsonl"
    if EVAL_ROOT is not None
    else _PACKAGE_CORPUS_PATH
)
DATASET_PATHS: dict[str, PathLike] = (
    {
        "intent-v1": EVAL_ROOT / "data" / "intent-v1.jsonl",
        "retrieval-v1": EVAL_ROOT / "data" / "retrieval-v1.jsonl",
        "dedup-v1": EVAL_ROOT / "data" / "dedup-v1.jsonl",
        "claims-v1": EVAL_ROOT / "data" / "claims-v1.jsonl",
        "e2e-v1": EVAL_ROOT / "data" / "e2e-v1.jsonl",
    }
    if EVAL_ROOT is not None
    else _PACKAGE_DATASET_PATHS
)
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
    "claims-v1": "932c1ac1983b311e905f6d642aef1b6efe6181ba845365cc02005aab53d0a12f",
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
_DEDUP_ALLOWED_FIELDS = set(_DEDUP_SCALAR_FIELDS) | {"claim_supporting_texts"}
_INTENT_NORMAL_EXPECTED_FIELDS = {
    "topics",
    "expanded_terms",
    "regions",
    "published_from",
    "published_to",
    "min_budget_minor_units",
    "max_budget_minor_units",
    "schedule_cron",
    "schedule_timezone",
}
_INTENT_ERROR_EXPECTED_FIELDS = {"error", "message"}
_INTENT_METADATA_FIELDS = {"case_type", "clock"}
_RETRIEVAL_FILTER_FIELDS = {"regions"}
_DEDUP_METADATA_FIELDS = {"case_type"}
_CLAIM_FIELDS = {"text", "citation_ids"}
_E2E_USAGE_FIELDS = {"prompt", "completion"}


def _read_jsonl(path: Path | Traversable) -> list[dict[str, Any]]:
    try:
        if not path.is_file():
            raise DatasetError(f"missing committed dataset: {path}")
        payload = path.read_bytes() if isinstance(path, Path) else path.read_bytes()
        text = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("utf-8")
    except DatasetError:
        raise
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


def _check_id(item: dict[str, Any], path: Path | Traversable, index: int) -> str:
    record_id = item.get("id")
    if not isinstance(record_id, str) or not record_id.startswith("eval-"):
        raise DatasetError(f"{path}:{index} must have an eval-* id")
    return record_id


def _check_synthetic(item: dict[str, Any], path: Path | Traversable, index: int) -> None:
    source = item.get("source")
    if source != "synthetic_demo":
        raise DatasetError(f"{path}:{index} must use source=synthetic_demo")
    if "external_id" in item:
        external_id = item["external_id"]
        if not isinstance(external_id, str) or not external_id.startswith("eval-"):
            raise DatasetError(f"{path}:{index} external_id must start with eval-")
    for field in ("canonical_url", "source_url"):
        url = item.get(field)
        if url is None:
            continue
        if not isinstance(url, str):
            raise DatasetError(f"{path}:{index} {field} must be a string")
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port
            username = parsed.username
            password = parsed.password
        except (TypeError, ValueError) as error:
            raise DatasetError(f"{path}:{index} has an invalid synthetic URL") from error
        if (
            parsed.scheme != "https"
            or hostname != "example.invalid"
            or username is not None
            or password is not None
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise DatasetError(f"{path}:{index} has a non-reserved synthetic URL")


def _check_nested_synthetic(value: Any, path: Path | Traversable, index: int) -> None:
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


def _is_nonnegative_int_or_none(value: Any) -> bool:
    return value is None or _is_nonnegative_int(value)


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _nullable_string(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _timezone_aware_datetime(value: Any) -> bool:
    if value is None or not isinstance(value, str):
        return value is None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _check_allowed_fields(
    value: dict[str, Any],
    allowed: set[str],
    path: Path | Traversable,
    index: int,
    location: str,
) -> None:
    unknown = sorted(str(field) for field in value if field not in allowed)
    if unknown:
        raise DatasetError(
            f"{path}:{index} {location} has unknown fields: {', '.join(unknown)}"
        )


def _validate_dedup_notice(
    value: Any, path: Path | Traversable, index: int, side: str
) -> None:
    if not isinstance(value, dict):
        raise DatasetError(f"{path}:{index} {side} must be an object")
    _check_allowed_fields(value, _DEDUP_ALLOWED_FIELDS, path, index, side)
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


def _validate_record_shape(
    item: dict[str, Any], path: Path | Traversable, index: int, schema_name: str
) -> None:
    """Enforce the explicit, nested JSONL schema used by each dataset."""
    _check_allowed_fields(item, REQUIRED_FIELDS[schema_name], path, index, schema_name)
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
        metadata = item.get("metadata")
        checks = {
            "source_url": isinstance(item.get("source_url"), str),
            "request": isinstance(item.get("request"), str),
            "expected": isinstance(expected, dict),
            "metadata": isinstance(metadata, dict),
        }
        if isinstance(expected, dict):
            is_error = "error" in expected
            expected_fields = (
                _INTENT_ERROR_EXPECTED_FIELDS if is_error else _INTENT_NORMAL_EXPECTED_FIELDS
            )
            _check_allowed_fields(expected, expected_fields, path, index, "expected")
            checks.update(
                {
                    "expected.error/message": is_error
                    and isinstance(expected.get("error"), str)
                    and isinstance(expected.get("message"), str)
                    if is_error
                    else True,
                    "expected.normal_fields": is_error
                    or (
                        set(expected) == _INTENT_NORMAL_EXPECTED_FIELDS
                        and _string_list(expected.get("topics"))
                        and _string_list(expected.get("expanded_terms"))
                        and _string_list(expected.get("regions"))
                        and _timezone_aware_datetime(expected.get("published_from"))
                        and _timezone_aware_datetime(expected.get("published_to"))
                        and _is_nonnegative_int_or_none(expected.get("min_budget_minor_units"))
                        and _is_nonnegative_int_or_none(expected.get("max_budget_minor_units"))
                        and _nullable_string(expected.get("schedule_cron"))
                        and _nullable_string(expected.get("schedule_timezone"))
                    ),
                    "expected.error_fields": not is_error
                    or set(expected) == _INTENT_ERROR_EXPECTED_FIELDS,
                }
            )
        if isinstance(metadata, dict):
            _check_allowed_fields(metadata, _INTENT_METADATA_FIELDS, path, index, "metadata")
            checks["metadata.fields"] = set(metadata) == _INTENT_METADATA_FIELDS and all(
                isinstance(metadata.get(field), str) for field in _INTENT_METADATA_FIELDS
            )
    elif schema_name == "retrieval-v1":
        filters = item.get("filters")
        checks = {
            "source_url": isinstance(item.get("source_url"), str),
            "query": isinstance(item.get("query"), str),
            "filters": isinstance(filters, dict),
            "relevant_ids": _string_list(item.get("relevant_ids")),
            "expected_top_k": isinstance(item.get("expected_top_k"), int)
            and not isinstance(item.get("expected_top_k"), bool)
            and item.get("expected_top_k") == 10,
        }
        if isinstance(filters, dict):
            _check_allowed_fields(filters, _RETRIEVAL_FILTER_FIELDS, path, index, "filters")
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
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            _check_allowed_fields(metadata, _DEDUP_METADATA_FIELDS, path, index, "metadata")
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
            for claim in claims:
                if isinstance(claim, dict):
                    _check_allowed_fields(claim, _CLAIM_FIELDS, path, index, "claims item")
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
        if isinstance(usage, dict):
            _check_allowed_fields(usage, _E2E_USAGE_FIELDS, path, index, "usage")
    else:  # pragma: no cover - guarded by the constant schema map
        checks = {}
    invalid = sorted(field for field, valid in checks.items() if not valid)
    if invalid:
        raise DatasetError(f"{path}:{index} has invalid fields: {', '.join(invalid)}")


def _validate_records(
    records: list[dict[str, Any]],
    path: Path | Traversable,
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
    for item in loaded["claims-v1"]:
        if item["notice_id"] not in corpus_ids:
            raise DatasetError(f"claims case {item['id']} references unknown corpus ID")
        evidence_ids = set(item["evidence_ids"])
        for claim in item["claims"]:
            citation_ids = set(claim["citation_ids"])
            if not citation_ids.issubset(evidence_ids):
                raise DatasetError(
                    f"claims case {item['id']} cites evidence outside its evidence_ids"
                )
    for item in loaded["e2e-v1"]:
        if not set(item["expected_notice_ids"]).issubset(corpus_ids):
            raise DatasetError(f"e2e case {item['id']} references unknown corpus ID")


def validate_generated_bundle(
    corpus: list[dict[str, Any]], datasets: dict[str, list[dict[str, Any]]]
) -> None:
    """Validate an in-memory bundle before a builder writes any artifact."""
    if set(datasets) != set(DATASET_PATHS):
        raise DatasetError("generated datasets do not match the committed dataset manifest")
    _validate_bundle({"corpus": corpus, **datasets}, minimums=True)


def _canonical_bytes(path: Path | Traversable) -> bytes:
    if not path.is_file():
        raise DatasetError(f"missing committed dataset: {path}")
    try:
        payload = path.read_bytes() if isinstance(path, Path) else path.read_bytes()
        return payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    except OSError as error:
        raise DatasetError(f"unable to hash dataset {path}: {error}") from error


def _is_default_paths(
    corpus_path: Path | Traversable, dataset_paths: dict[str, Path | Traversable]
) -> bool:
    return corpus_path == CORPUS_PATH and dataset_paths == DATASET_PATHS


def _verify_expected_hashes(hashes: dict[str, str]) -> None:
    if hashes != EXPECTED_DATASET_HASHES:
        raise DatasetError(
            "committed evaluation dataset hash does not match the approved manifest"
        )


def validate_committed_datasets(
    *,
    corpus_path: Path | Traversable | None = None,
    dataset_paths: dict[str, Path | Traversable] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Load committed checkout files or packaged resources and enforce invariants."""
    if corpus_path is None or dataset_paths is None:
        default_corpus, default_datasets = _dataset_sources()
        corpus_path = default_corpus if corpus_path is None else corpus_path
        dataset_paths = default_datasets if dataset_paths is None else dataset_paths
    corpus = _read_jsonl(corpus_path)
    loaded: dict[str, list[dict[str, Any]]] = {"corpus": corpus}
    for name, path in dataset_paths.items():
        loaded[name] = _read_jsonl(path)
    if set(dataset_paths) != set(DATASET_PATHS):
        raise DatasetError("dataset paths do not match the committed dataset manifest")
    _validate_bundle(loaded, minimums=True)
    if _is_default_paths(corpus_path, dataset_paths):
        _verify_expected_hashes(
            dataset_hashes(corpus_path=corpus_path, dataset_paths=dataset_paths)
        )
    return loaded


def dataset_hashes(
    *,
    corpus_path: Path | Traversable = CORPUS_PATH,
    dataset_paths: dict[str, Path | Traversable] = DATASET_PATHS,
) -> dict[str, str]:
    """Return SHA-256 hashes of canonical LF-normalized files in filename order."""
    paths = {"corpus": corpus_path, **dataset_paths}
    return {
        name: hashlib.sha256(_canonical_bytes(paths[name])).hexdigest()
        for name in sorted(paths)
    }


def _dataset_sources() -> tuple[Path | Traversable, dict[str, Path | Traversable]]:
    """Prefer source-checkout eval files, falling back to bundled package resources."""
    if PROJECT_ROOT is not None and CORPUS_PATH.is_file() and all(
        path.is_file() for path in DATASET_PATHS.values()
    ):
        return CORPUS_PATH, DATASET_PATHS
    return _PACKAGE_CORPUS_PATH, _PACKAGE_DATASET_PATHS


def load_datasets() -> dict[str, list[dict[str, Any]]]:
    """Load validated checkout fixtures or package resources without regeneration."""
    corpus_path, dataset_paths = _dataset_sources()
    loaded = validate_committed_datasets(
        corpus_path=corpus_path,
        dataset_paths=dataset_paths,
    )
    if corpus_path != CORPUS_PATH:
        _verify_expected_hashes(
            dataset_hashes(corpus_path=corpus_path, dataset_paths=dataset_paths)
        )
    return loaded


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
