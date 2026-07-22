"""Deterministic, offline evaluation runner over committed JSONL data."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import platform
import subprocess
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bidscope.clock import FixedClock, SystemClock
from bidscope.evaluation.datasets import (
    PROJECT_ROOT,
    DatasetError,
    _dataset_sources,
    dataset_hashes,
    load_datasets,
)
from bidscope.evaluation.metrics import (
    binary_classification_metrics,
    citation_correctness,
    citation_coverage,
    cny_cost,
    field_exact_match,
    field_macro_f1,
    multiclass_classification_metrics,
    ndcg_at_k,
    percentile_latency,
    recall_at_k,
    task_success_rate,
    total_tokens,
)
from bidscope.llm.fake import FakeIntentModel
from bidscope.retrieval.deduplication import NoticeView, classify_duplicate

PRICING_SNAPSHOT_DATE = "2026-07-18"
DATABASE_FIXTURE_VERSION = "synthetic-notices-v1"
MODEL_NAME = "fake-deterministic"
MODEL_PROVIDER = "offline"
PRICING_CNY_PER_MILLION = {"prompt": 0.0, "completion": 0.0}
MEASUREMENT_MODE = {
    "intent": "fixture_consistency",
    "retrieval": "fixture_consistency",
    "dedup": "fixture_consistency",
    "claims": "fixture_consistency",
    "e2e": "fixture_consistency",
    "latency": "fixture_consistency",
    "tokens": "fixture_consistency",
    "cost": "fixture_consistency",
}
TARGETS = {
    "intent_macro_f1": 0.90,
    "retrieval_recall_at_10": 0.85,
    "retrieval_ndcg_at_10": 0.85,
    "dedup_f1": 0.90,
    "citation_coverage": 1.0,
    "citation_correctness": 0.95,
    "task_success_rate": 0.95,
    "latency_p95_ms": 15_000.0,
    "cost_cny": 0.10,
}


class EvaluationExecutionError(RuntimeError):
    """Raised for failures while executing deterministic evaluation logic."""


def _git_value(*args: str) -> str:
    """Return git provenance when available, or a bounded unavailable marker."""
    if PROJECT_ROOT is None:
        return "unavailable"
    try:
        value = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"
    return value or "unavailable"


_CLOCK = SystemClock()


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC time through the Clock boundary."""
    return _CLOCK.now()


def _intent_projection(value: Any) -> dict[str, Any]:
    return {
        "topics": value.topics,
        "expanded_terms": value.expanded_terms,
        "regions": value.regions,
        "published_from": value.published_from.isoformat() if value.published_from else None,
        "published_to": value.published_to.isoformat() if value.published_to else None,
        "min_budget_minor_units": value.min_budget.minor_units if value.min_budget else None,
        "max_budget_minor_units": value.max_budget.minor_units if value.max_budget else None,
        "schedule_cron": value.schedule.cron_expression if value.schedule else None,
        "schedule_timezone": value.schedule.timezone if value.schedule else None,
    }


async def _run_intent_cases(
    cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
    model = FakeIntentModel()
    expected: list[dict[str, Any]] = []
    predicted: list[dict[str, Any]] = []
    usages: list[Any] = []
    for case in cases:
        # A failed invocation must not inherit the previous successful receipt.
        model._last_usage = None
        expected_value = case["expected"]
        if "error" in expected_value:
            expected.append({"error": expected_value["error"]})
            try:
                await model.parse(case["request"], FixedClock(datetime(2026, 7, 18, 9, tzinfo=UTC)))
            except ValueError as error:
                predicted.append({"error": type(error).__name__})
            else:  # pragma: no cover - dataset/model contract violation
                predicted.append({"error": None})
        else:
            expected.append(expected_value)
            try:
                value = await model.parse(
                    case["request"], FixedClock(datetime(2026, 7, 18, 9, tzinfo=UTC))
                )
            except ValueError as error:  # pragma: no cover - dataset/model contract violation
                raise EvaluationExecutionError(f"intent case failed: {case['id']}") from error
            predicted.append(_intent_projection(value))
            if model.last_usage is not None:
                usages.append(model.last_usage)
    return expected, predicted, usages


def _rank_retrieval(query: str, regions: list[str], corpus: list[dict[str, Any]]) -> list[str]:
    filtered = [
        (index, notice)
        for index, notice in enumerate(corpus)
        if not regions or notice["region"] in regions
    ]
    query_terms = [term for term in query.split() if term]
    scored: list[tuple[int, int, str]] = []
    for index, notice in filtered:
        haystack = f"{notice['title']} {notice['content']}"
        score = sum(haystack.count(term) for term in query_terms) if query_terms else 0
        if query and query in haystack:
            score += 100
        scored.append((-score, index, notice["id"]))
    scored.sort()
    return [notice_id for _, _, notice_id in scored]


def _notice_view(record: dict[str, Any]) -> NoticeView:
    from datetime import datetime

    deadline = record.get("deadline")
    return NoticeView(
        source=record["source"],
        external_id=record["external_id"],
        canonical_url=record["canonical_url"],
        project_number=record.get("project_number"),
        content_hash=record["content_hash"],
        title=record.get("title"),
        purchaser=record.get("purchaser"),
        region=record.get("region"),
        budget_minor_units=record.get("budget_minor_units"),
        budget_currency=record.get("budget_currency"),
        deadline=datetime.fromisoformat(deadline) if deadline else None,
        procurement_scope=record.get("procurement_scope"),
        cancellation=bool(record.get("cancellation", False)),
        claim_supporting_texts=tuple(record.get("claim_supporting_texts", ())),
    )


def _metric_result(value: float, target: float, *, lower_is_better: bool = False) -> dict[str, Any]:
    passed = value <= target if lower_is_better else value >= target
    return {"value": value, "target": target, "passed": passed}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _run_intent_cases_sync(cases: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[Any]
]:
    """Run intent cases without clearing an existing thread event loop."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            previous_loop = None
    try:
        return asyncio.run(_run_intent_cases(cases))
    finally:
        if previous_loop is not None and not previous_loop.is_closed():
            asyncio.set_event_loop(previous_loop)


def _hash_selected_sources(
    corpus_path: Any, dataset_paths: dict[str, Any]
) -> dict[str, str]:
    """Hash the same selected resources as loading, while supporting test doubles."""
    parameters = inspect.signature(dataset_hashes).parameters
    accepts_keywords = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    accepts_sources = accepts_keywords or {"corpus_path", "dataset_paths"}.issubset(parameters)
    if accepts_sources:
        return dataset_hashes(corpus_path=corpus_path, dataset_paths=dataset_paths)
    return dataset_hashes()


def run_deterministic(*, output: Path | None = None) -> dict[str, Any]:
    """Run deterministic evaluation without regenerating or mutating datasets."""
    started_at = _utc_now()
    started = time.perf_counter()
    try:
        corpus_path, dataset_paths = _dataset_sources()
        datasets = load_datasets()
        hashes = _hash_selected_sources(corpus_path, dataset_paths)
        intent_expected, intent_predicted, intent_usages = _run_intent_cases_sync(
            datasets["intent-v1"]
        )

        valid_expected = [item for item in intent_expected if "error" not in item]
        valid_predicted = [
            predicted
            for expected, predicted in zip(intent_expected, intent_predicted, strict=True)
            if "error" not in expected
        ]
        exact = field_exact_match(valid_expected, valid_predicted)
        macro = field_macro_f1(valid_expected, valid_predicted)
        error_cases = [expected for expected in intent_expected if "error" in expected]
        intent_error_accuracy = (
            sum(
                expected.get("error") == predicted.get("error")
                for expected, predicted in zip(intent_expected, intent_predicted, strict=True)
                if "error" in expected
            )
            / len(error_cases)
            if error_cases
            else 0.0
        )

        corpus = datasets["corpus"]
        retrieval_recalls: list[float] = []
        retrieval_ndcgs: list[float] = []
        for case in datasets["retrieval-v1"]:
            ranked = _rank_retrieval(case["query"], case["filters"].get("regions", []), corpus)
            relevant = set(case["relevant_ids"])
            top_k = case["expected_top_k"]
            retrieval_recalls.append(recall_at_k(relevant, ranked, k=top_k))
            retrieval_ndcgs.append(ndcg_at_k(relevant, ranked, k=top_k))

        dedup_actual: list[bool] = []
        dedup_predicted: list[bool] = []
        dedup_labels_actual: list[str] = []
        dedup_labels_predicted: list[str] = []
        for case in datasets["dedup-v1"]:
            decision = classify_duplicate(
                _notice_view(case["left"]), _notice_view(case["right"])
            ).decision
            expected_decision = case["expected_decision"]
            dedup_labels_actual.append(expected_decision)
            dedup_labels_predicted.append(decision)
            dedup_actual.append(expected_decision == "exact")
            dedup_predicted.append(decision == "exact")
        dedup = binary_classification_metrics(dedup_actual, dedup_predicted)
        dedup_multiclass = multiclass_classification_metrics(
            dedup_labels_actual, dedup_labels_predicted
        )

        claims = [claim for case in datasets["claims-v1"] for claim in case["claims"]]
        coverage = citation_coverage(claims)
        claim_correctness = [
            citation_correctness([claim], set(case["evidence_ids"]))
            for case in datasets["claims-v1"]
            for claim in case["claims"]
        ]
        correctness = _mean(claim_correctness)
        support_accuracy = _mean(
            [
                all(
                    bool(claim.get("citation_ids"))
                    and set(claim.get("citation_ids", ())).issubset(set(case["evidence_ids"]))
                    for claim in case["claims"]
                )
                == case["expected_supported"]
                for case in datasets["claims-v1"]
            ]
        )

        scenarios = datasets["e2e-v1"]
        success = task_success_rate(scenarios)
        latencies = [float(scenario["latency_ms"]) for scenario in scenarios]
        latency = percentile_latency(latencies)
        usages = intent_usages + [
            {
                "prompt_tokens": int(scenario["usage"]["prompt"]),
                "completion_tokens": int(scenario["usage"]["completion"]),
            }
            for scenario in scenarios
        ]
        tokens = total_tokens(usages)
        cost = cny_cost(
            {"prompt": tokens["prompt"], "completion": tokens["completion"]},
            PRICING_CNY_PER_MILLION,
        )
        intent_macro_f1 = _mean(list(macro.values()))
        metrics: dict[str, Any] = {
            "intent_field_exact_match": exact,
            "intent_field_macro_f1": macro,
            "intent_macro_f1": intent_macro_f1,
            "intent_error_accuracy": intent_error_accuracy,
            "retrieval_recall_at_10": _mean(retrieval_recalls),
            "retrieval_ndcg_at_10": _mean(retrieval_ndcgs),
            "dedup_precision": dedup["precision"],
            "dedup_recall": dedup["recall"],
            "dedup_f1": dedup["f1"],
            "dedup_label_accuracy": dedup_multiclass["accuracy"],
            "dedup_macro_precision": dedup_multiclass["macro"]["precision"],
            "dedup_macro_recall": dedup_multiclass["macro"]["recall"],
            "dedup_macro_f1": dedup_multiclass["macro"]["f1"],
            "citation_coverage": coverage,
            "citation_correctness": correctness,
            "citation_support_accuracy": support_accuracy,
            "task_success_rate": success,
            "latency_p50_ms": latency["p50"],
            "latency_p95_ms": latency["p95"],
            "tokens_prompt": tokens["prompt"],
            "tokens_completion": tokens["completion"],
            "tokens_total": tokens["total"],
            "cost_cny": cost,
        }
        target_results = {
            name: _metric_result(
                float(metrics[name]),
                target,
                lower_is_better=name in {"latency_p95_ms", "cost_cny"},
            )
            for name, target in TARGETS.items()
        }
    except DatasetError:
        raise
    except EvaluationExecutionError:
        raise
    except Exception as error:  # noqa: BLE001 — normalize execution failures for CLI
        raise EvaluationExecutionError(str(error)) from error

    commit = _git_value("rev-parse", "HEAD")
    dirty_status = _git_value("status", "--porcelain")
    dirty = dirty_status != "unavailable" and bool(dirty_status)
    result: dict[str, Any] = {
        "schema_version": "evaluation-result-v1",
        "mode": "deterministic",
        "status": "completed",
        "started_at": started_at.isoformat(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "git_commit": commit,
        "working_tree_dirty": dirty,
        "dataset_hashes": hashes,
        "dataset_counts": {name: len(records) for name, records in datasets.items()},
        "model": {"name": MODEL_NAME, "provider": MODEL_PROVIDER},
        "model_name": MODEL_NAME,
        "provider": MODEL_PROVIDER,
        "pricing_snapshot": {
            "date": PRICING_SNAPSHOT_DATE,
            "currency": "CNY",
            "cny_per_million_tokens": PRICING_CNY_PER_MILLION,
        },
        "pricing_snapshot_date": PRICING_SNAPSHOT_DATE,
        "database_fixture_version": DATABASE_FIXTURE_VERSION,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "app_mode": os.environ.get("BIDSCOPE_APP_MODE", "demo"),
            "network": "disabled",
        },
        "metrics": metrics,
        "measurement_mode": MEASUREMENT_MODE,
        "latency_ms": latency,
        "tokens": tokens,
        "cost_cny": cost,
        "targets": TARGETS,
        "target_results": target_results,
        "target_pass": all(item["passed"] for item in target_results.values()),
    }
    if output is not None:
        output_path = (
            output
            if output.is_absolute() or PROJECT_ROOT is None
            else PROJECT_ROOT / output
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        output_path.write_bytes(payload.encode("utf-8"))
    return result


__all__ = [
    "DATABASE_FIXTURE_VERSION",
    "EvaluationExecutionError",
    "MODEL_NAME",
    "MODEL_PROVIDER",
    "PRICING_SNAPSHOT_DATE",
    "run_deterministic",
]
