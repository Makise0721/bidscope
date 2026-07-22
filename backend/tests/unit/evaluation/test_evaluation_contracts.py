"""RED-phase regression contracts for Task 18 evaluation review findings."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from bidscope.evaluation import runner
from bidscope.evaluation.datasets import (
    DATASET_PATHS,
    DatasetError,
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


def test_validator_rejects_non_numeric_corpus_budget(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["corpus"][0]["budget_minor_units"] = "not-a-number"

    _assert_bundle_rejected(tmp_path, mutate)


def test_validator_rejects_string_claim_citation_ids(tmp_path: Path) -> None:
    def mutate(datasets: dict[str, list[dict[str, Any]]]) -> None:
        datasets["claims-v1"][0]["claims"][0]["citation_ids"] = "eval-evidence-001"

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

    required_metrics = {"claims", "e2e", "latency", "tokens", "cost"}
    provenance = result.get("measurement_mode", result.get("metric_provenance"))
    assert isinstance(provenance, dict)
    assert required_metrics <= set(provenance)
    assert all(value in {"execution", "fixture_consistency"} for value in provenance.values())


def test_runner_does_not_count_stale_usage_after_failed_intent_parse() -> None:
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

    _, _, usages = asyncio.run(_run_intent_cases(cases))

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


def test_ndcg_does_not_exceed_one_for_duplicate_ranked_ids() -> None:
    assert ndcg_at_k({"notice-1"}, ["notice-1", "notice-1"], k=2) == pytest.approx(1.0)


def test_binary_classification_metrics_reject_unequal_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        binary_classification_metrics([True, False], [True])
