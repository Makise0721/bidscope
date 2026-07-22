"""RED-phase regression contracts for Task 18 evaluation review findings."""

from __future__ import annotations

import copy
import importlib.util
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from bidscope.evaluation import datasets as dataset_module
from bidscope.evaluation import runner
from bidscope.evaluation.datasets import (
    _PACKAGE_CORPUS_PATH,
    _PACKAGE_DATASET_PATHS,
    DATASET_PATHS,
    DatasetError,
    _dataset_sources,
    dataset_hashes,
    load_datasets,
    validate_committed_datasets,
)
from bidscope.evaluation.metrics import binary_classification_metrics, ndcg_at_k

_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "build_eval_data", Path(__file__).resolve().parents[4] / "scripts" / "build_eval_data.py"
)
assert _BUILDER_SPEC is not None and _BUILDER_SPEC.loader is not None
_BUILDER = importlib.util.module_from_spec(_BUILDER_SPEC)
_BUILDER_SPEC.loader.exec_module(_BUILDER)
_write_jsonl = _BUILDER._write_jsonl


def _write_bundle(
    root: Path, datasets: dict[str, list[dict[str, Any]]]
) -> tuple[Path, dict[str, Path]]:
    corpus_path = root / "corpus.jsonl"
    _write_jsonl(corpus_path, datasets["corpus"])
    dataset_paths = {
        name: root / f"{name}.jsonl" for name in DATASET_PATHS
    }
    for name, path in dataset_paths.items():
        _write_jsonl(path, datasets[name])
    return corpus_path, dataset_paths


def _assert_bundle_rejected(
    tmp_path: Path, mutate: Callable[[dict[str, list[dict[str, Any]]]], None]
) -> None:
    datasets = copy.deepcopy(load_datasets())
    mutate(datasets)
    corpus_path, dataset_paths = _write_bundle(tmp_path, datasets)

    with pytest.raises(DatasetError):
        validate_committed_datasets(
            corpus_path=corpus_path,
            dataset_paths=dataset_paths,
        )


def test_dataset_hashes_are_independent_of_jsonl_line_endings(tmp_path: Path) -> None:
    lf_path = tmp_path / "lf.jsonl"
    crlf_path = tmp_path / "crlf.jsonl"
    payload = b'{"id":"eval-line-ending"}\n'
    lf_path.write_bytes(payload)
    crlf_path.write_bytes(payload.replace(b"\n", b"\r\n"))

    lf_hashes = dataset_hashes(
        corpus_path=lf_path,
        dataset_paths={"intent-v1": lf_path},
    )
    crlf_hashes = dataset_hashes(
        corpus_path=crlf_path,
        dataset_paths={"intent-v1": crlf_path},
    )

    assert lf_hashes == crlf_hashes


def test_builder_writes_jsonl_with_lf_bytes(tmp_path: Path) -> None:
    output = tmp_path / "records.jsonl"
    _write_jsonl(output, [{"id": "eval-line-ending", "value": "ok"}])

    payload = output.read_bytes()

    assert b"\r\n" not in payload
    assert payload.endswith(b"\n")


def test_loader_and_runner_hash_share_package_fallback_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source-checkout"
    source_corpus = source_root / "eval" / "corpus" / "synthetic-notices-v1.jsonl"
    source_dataset_paths = {
        name: source_root / "eval" / "data" / f"{name}.jsonl" for name in DATASET_PATHS
    }
    source_corpus.parent.mkdir(parents=True)
    source_corpus.write_bytes(_PACKAGE_CORPUS_PATH.read_bytes())
    for name, path in source_dataset_paths.items():
        if name != "intent-v1":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_PACKAGE_DATASET_PATHS[name].read_bytes())

    monkeypatch.setattr(dataset_module, "PROJECT_ROOT", source_root)
    monkeypatch.setattr(dataset_module, "CORPUS_PATH", source_corpus)
    monkeypatch.setattr(dataset_module, "DATASET_PATHS", source_dataset_paths)

    selected_corpus, selected_datasets = _dataset_sources()
    assert selected_corpus == _PACKAGE_CORPUS_PATH
    assert selected_datasets == _PACKAGE_DATASET_PATHS
    loaded = load_datasets()
    assert loaded["intent-v1"]

    hash_calls: list[tuple[Any, dict[str, Any]]] = []

    def hash_selected_sources(
        *,
        corpus_path: Any | None = None,
        dataset_paths: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        if corpus_path is None or dataset_paths is None:
            corpus_path = source_corpus
            dataset_paths = source_dataset_paths
        hash_calls.append((corpus_path, dataset_paths))
        return dataset_hashes(corpus_path=corpus_path, dataset_paths=dataset_paths)

    monkeypatch.setattr(runner, "dataset_hashes", hash_selected_sources)
    result = runner.run_deterministic()

    assert hash_calls == [(selected_corpus, selected_datasets)]
    assert result["dataset_hashes"] == dataset_hashes(
        corpus_path=selected_corpus,
        dataset_paths=selected_datasets,
    )


def test_validator_rejects_non_numeric_corpus_budget(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["corpus"][0]["budget_minor_units"] = "not-a-number"

    _assert_bundle_rejected(tmp_path, mutate)


@pytest.mark.parametrize(
    ("dataset_name", "record_path"),
    [
        ("corpus", ("budget_minor_units",)),
        ("dedup-v1", ("left", "budget_minor_units")),
    ],
)
def test_validator_rejects_fractional_budget_minor_units(
    tmp_path: Path, dataset_name: str, record_path: tuple[str, ...]
) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        value: Any = datasets[dataset_name][0]
        for field in record_path[:-1]:
            value = value[field]
        value[record_path[-1]] = 1.5

    _assert_bundle_rejected(tmp_path, mutate)


@pytest.mark.parametrize(
    ("dataset_name", "record_path", "deadline"),
    [
        ("corpus", ("deadline",), "not-a-time"),
        ("dedup-v1", ("left", "deadline"), "2026-01-01T00:00:00"),
    ],
)
def test_validator_rejects_invalid_or_naive_deadlines(
    tmp_path: Path, dataset_name: str, record_path: tuple[str, ...], deadline: str
) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        value: Any = datasets[dataset_name][0]
        for field in record_path[:-1]:
            value = value[field]
        value[record_path[-1]] = deadline

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_normalizes_huge_json_integer_to_dataset_error(tmp_path: Path) -> None:
    datasets = copy.deepcopy(load_datasets())
    corpus_path, dataset_paths = _write_bundle(tmp_path, datasets)
    corpus_payload = corpus_path.read_text(encoding="utf-8")
    huge_integer = "9" * 5000
    corpus_path.write_text(
        corpus_payload.replace(
            '"budget_minor_units": 100000',
            f'"budget_minor_units": {huge_integer}',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(DatasetError):
        validate_committed_datasets(
            corpus_path=corpus_path,
            dataset_paths=dataset_paths,
        )


def test_generated_bundle_normalizes_huge_numeric_budget_to_dataset_error() -> None:
    datasets = copy.deepcopy(load_datasets())
    datasets["corpus"][0]["budget_minor_units"] = 10**5000

    with pytest.raises(DatasetError):
        dataset_module.validate_generated_bundle(datasets["corpus"], {
            name: datasets[name] for name in DATASET_PATHS
        })


def test_validator_rejects_huge_e2e_usage_integer(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["e2e-v1"][0]["usage"]["prompt"] = 10**400

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_unknown_top_level_intent_field(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["intent-v1"][0]["unexpected"] = "reject me"

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_unknown_nested_intent_field(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["intent-v1"][0]["expected"]["unexpected"] = "reject me"

    _assert_bundle_rejected(tmp_path, mutate)


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [
        ("error", {"type": "ValueError"}),
        ("message", ["not-a-message"]),
        ("published_from", {"date": "2026-07-17T00:00:00+00:00"}),
        ("published_to", ["2026-07-18T00:00:00+00:00"]),
        ("min_budget_minor_units", "not-a-budget"),
        ("max_budget_minor_units", {"minor_units": 10_000}),
        ("schedule_cron", {"expression": "0 9 * * 1"}),
        ("schedule_timezone", ["Asia/Shanghai"]),
    ],
)
def test_validator_rejects_malformed_intent_expected_scalar_fields(
    tmp_path: Path, field: str, malformed_value: Any
) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["intent-v1"][0]["expected"][field] = malformed_value

    _assert_bundle_rejected(tmp_path, mutate)


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [("case_type", {"kind": "template"}), ("clock", ["2026-07-18T09:00:00+00:00"])],
)
def test_validator_rejects_malformed_intent_metadata_scalar_type(
    tmp_path: Path, field: str, malformed_value: Any
) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["intent-v1"][0]["metadata"][field] = malformed_value

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_requires_retrieval_expected_top_k_of_ten(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["retrieval-v1"][0]["expected_top_k"] = 5

    _assert_bundle_rejected(tmp_path, mutate)


@pytest.mark.parametrize(
    ("dataset_name", "record_path"),
    [
        ("corpus", ("external_id",)),
        ("dedup-v1", ("left", "external_id")),
        ("dedup-v1", ("right", "external_id")),
    ],
)
def test_validator_requires_eval_prefix_for_external_ids(
    tmp_path: Path, dataset_name: str, record_path: tuple[str, ...]
) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        value: Any = datasets[dataset_name][0]
        for field in record_path[:-1]:
            value = value[field]
        value[record_path[-1]] = "official-001"

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_string_claim_citation_ids(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["claims-v1"][0]["claims"][0]["citation_ids"] = "eval-evidence-001"

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_supported_claim_citing_unknown_evidence(
    tmp_path: Path,
) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        claim_case = datasets["claims-v1"][0]
        claim_case["expected_supported"] = True
        claim_case["claims"][0]["citation_ids"] = ["eval-evidence-does-not-exist"]
        assert claim_case["evidence_ids"] == ["eval-evidence-001"]

    _assert_bundle_rejected(tmp_path, mutate)


@pytest.mark.parametrize(
    "metadata",
    [{}, {"case_type": ["bad"]}],
    ids=["missing-case-type", "invalid-case-type-type"],
)
def test_validator_rejects_invalid_dedup_metadata(
    tmp_path: Path, metadata: dict[str, Any]
) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["dedup-v1"][0]["metadata"] = metadata

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_list_dedup_content_hash(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["dedup-v1"][0]["left"]["content_hash"] = ["not-a-hash"]

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_non_list_retrieval_regions(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["retrieval-v1"][0]["filters"]["regions"] = {"region": "四川"}

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_non_string_e2e_notice_ids(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["e2e-v1"][0]["expected_notice_ids"] = [{"id": "eval-notice-001"}]

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_claims_notice_id_outside_corpus(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["claims-v1"][0]["notice_id"] = "eval-unknown-claim-notice"

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_e2e_expected_notice_id_outside_corpus(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["e2e-v1"][0]["expected_notice_ids"] = ["eval-unknown-e2e-notice"]

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_synthetic_url_userinfo(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["claims-v1"][0]["source_url"] = "https://user:password@example.invalid/claim/1"

    _assert_bundle_rejected(tmp_path, mutate)


def test_evaluation_result_discloses_metric_measurement_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets = copy.deepcopy(load_datasets())
    monkeypatch.setattr(runner, "load_datasets", lambda: datasets)
    monkeypatch.setattr(runner, "dataset_hashes", lambda: {})
    monkeypatch.setattr(runner, "_git_value", lambda *_args: "test-provenance")

    result = runner.run_deterministic()

    required_metrics = {
        "intent",
        "retrieval",
        "dedup",
        "claims",
        "e2e",
        "latency",
        "tokens",
        "cost",
    }
    provenance = result.get("measurement_mode", result.get("metric_provenance"))
    assert isinstance(provenance, dict)
    assert required_metrics <= set(provenance)
    assert provenance["intent"] == "fixture_consistency"
    assert provenance["retrieval"] == "fixture_consistency"
    assert provenance["dedup"] == "fixture_consistency"


@pytest.mark.asyncio
async def test_runner_completes_when_called_from_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets = copy.deepcopy(load_datasets())
    monkeypatch.setattr(runner, "load_datasets", lambda: datasets)
    monkeypatch.setattr(runner, "dataset_hashes", lambda: {})
    monkeypatch.setattr(runner, "_git_value", lambda *_args: "test-provenance")
    monkeypatch.setattr(
        runner,
        "_utc_now",
        lambda: datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
    )

    result = runner.run_deterministic()

    assert result["status"] == "completed"


def test_started_at_is_captured_before_evaluation_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    datasets = copy.deepcopy(load_datasets())
    entry_time = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    after_delay = datetime(2026, 7, 22, 10, 5, tzinfo=UTC)
    clock_reads = iter([entry_time, after_delay])
    monkeypatch.setattr(runner, "load_datasets", lambda: datasets)
    monkeypatch.setattr(runner, "dataset_hashes", lambda: {})
    monkeypatch.setattr(runner, "_git_value", lambda *_args: "test-provenance")
    monkeypatch.setattr(runner, "_utc_now", lambda: next(clock_reads), raising=False)

    original_run = runner.asyncio.run

    def delayed_run(awaitable: Any) -> Any:
        result = original_run(awaitable)
        return result

    monkeypatch.setattr(runner.asyncio, "run", delayed_run)
    result = runner.run_deterministic()

    assert result["started_at"] == entry_time.isoformat()
    assert result["started_at"] != after_delay.isoformat()


@pytest.mark.asyncio
async def test_runner_does_not_count_stale_usage_after_failed_intent_parse() -> None:
    from bidscope.evaluation.runner import _run_intent_cases

    cases = [
        {
            "id": "eval-intent-valid",
            "request": "查找四川服务器项目",
            "expected": {"topics": ["服务器"]},
        },
        {
            "id": "eval-intent-invalid",
            "request": "",
            "expected": {"error": "ValueError"},
        },
    ]

    _, _, usages = await _run_intent_cases(cases)

    assert len(usages) == 1
    assert usages[0].prompt_tokens == len(cases[0]["request"])


def test_citation_correctness_is_scoped_to_each_case(monkeypatch: pytest.MonkeyPatch) -> None:
    datasets = copy.deepcopy(load_datasets())
    datasets["claims-v1"] = [
        {
            "id": "eval-claim-a",
            "source": "synthetic_demo",
            "source_url": "https://example.invalid/claim/a",
            "notice_id": "eval-notice-001",
            "claims": [{"text": "claim A", "citation_ids": ["eval-evidence-b"]}],
            "evidence_ids": ["eval-evidence-a"],
            "expected_supported": False,
        },
        {
            "id": "eval-claim-b",
            "source": "synthetic_demo",
            "source_url": "https://example.invalid/claim/b",
            "notice_id": "eval-notice-002",
            "claims": [{"text": "claim B", "citation_ids": ["eval-evidence-b"]}],
            "evidence_ids": ["eval-evidence-b"],
            "expected_supported": True,
        },
    ]
    monkeypatch.setattr(runner, "load_datasets", lambda: datasets)
    monkeypatch.setattr(runner, "dataset_hashes", lambda: {})
    monkeypatch.setattr(runner, "_git_value", lambda *_args: "test-provenance")

    result = runner.run_deterministic()

    assert result["metrics"]["citation_correctness"] == pytest.approx(0.5)
    assert result["metrics"]["citation_support_accuracy"] == pytest.approx(1.0)


def test_runner_scores_prefixed_absent_claim_citation_as_incorrect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets = copy.deepcopy(load_datasets())
    claim_case = datasets["claims-v1"][0]
    claim_case["claims"][0]["citation_ids"] = ["eval-evidence-absent"]
    monkeypatch.setattr(runner, "load_datasets", lambda: datasets)
    monkeypatch.setattr(runner, "dataset_hashes", lambda: {})
    monkeypatch.setattr(runner, "_git_value", lambda *_args: "test-provenance")

    result = runner.run_deterministic()

    assert result["metrics"]["citation_correctness"] < 1.0
    assert result["metrics"]["citation_support_accuracy"] < 1.0


def test_ndcg_does_not_exceed_one_for_duplicate_ranked_ids() -> None:
    assert ndcg_at_k({"notice-1"}, ["notice-1", "notice-1"], k=2) == pytest.approx(1.0)


def test_binary_classification_metrics_reject_unequal_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        binary_classification_metrics([True, False], [True])
