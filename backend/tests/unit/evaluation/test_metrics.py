"""RED-phase tests for the deterministic Task 18 evaluation metrics."""

from __future__ import annotations

import math

import pytest
from bidscope.evaluation.metrics import (
    binary_classification_metrics,
    citation_correctness,
    citation_coverage,
    cny_cost,
    field_exact_match,
    field_macro_f1,
    ndcg_at_k,
    percentile_latency,
    recall_at_k,
    task_success_rate,
    total_tokens,
)

# These fixtures intentionally use ordinary mappings and sequences so metric
# functions remain independent of the versioned JSONL dataset representation.


def test_field_exact_match_scores_each_field_and_empty_cases() -> None:
    expected = [
        {"topics": ["智算中心"], "regions": ["四川", "重庆"], "min_budget": 5_000_000},
        {"topics": ["服务器"], "regions": [], "min_budget": None},
    ]
    predicted = [
        {"topics": ["智算中心"], "regions": ["四川", "重庆"], "min_budget": 5_000_000},
        {"topics": ["服务器"], "regions": ["四川"], "min_budget": 0},
    ]

    assert field_exact_match(expected, predicted) == {
        "topics": 1.0,
        "regions": 0.5,
        "min_budget": 0.5,
    }
    assert field_exact_match([], []) == {}


def test_field_macro_f1_scores_labels_and_empty_cases() -> None:
    expected = [
        {"region": "四川"},
        {"region": "重庆"},
        {"region": "四川"},
        {"region": "四川"},
    ]
    predicted = [
        {"region": "四川"},
        {"region": "四川"},
        {"region": "重庆"},
        {"region": "四川"},
    ]

    # Macro F1 averages F1 for the two observed labels: 2/3 for 四川 and 0
    # for 重庆, so the field score is 1/3.
    assert field_macro_f1(expected, predicted)["region"] == pytest.approx(1 / 3)
    assert field_macro_f1([], []) == {}


def test_recall_at_10_counts_relevant_items_in_ranked_results() -> None:
    relevant = {"n-1", "n-4", "n-11"}
    ranked = ["n-9", "n-4", "n-2", "n-1", "n-8", "n-3", "n-7", "n-6", "n-5", "n-10", "n-11"]

    assert recall_at_k(relevant, ranked, k=10) == pytest.approx(2 / 3)
    assert recall_at_k(set(), [], k=10) == 0.0
    assert recall_at_k({"n-1"}, [], k=10) == 0.0


def test_ndcg_at_10_uses_binary_relevance_and_handles_empty_results() -> None:
    relevant = {"n-1", "n-3"}
    ranked = ["n-2", "n-3", "n-8", "n-1"]

    # DCG = 1/log2(3) + 1/log2(5); IDCG = 1 + 1/log2(3).
    expected = (1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3))
    assert ndcg_at_k(relevant, ranked, k=10) == pytest.approx(expected)
    assert ndcg_at_k(set(), [], k=10) == 0.0
    assert ndcg_at_k({"n-1"}, [], k=10) == 0.0


def test_binary_classification_metrics_include_zero_positive_case() -> None:
    actual = [True, True, False, False]
    predicted = [True, False, True, False]

    assert binary_classification_metrics(actual, predicted) == {
        "precision": pytest.approx(0.5),
        "recall": pytest.approx(0.5),
        "f1": pytest.approx(0.5),
    }
    assert binary_classification_metrics([False, False], [True, False]) == {
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
    }
    assert binary_classification_metrics([], []) == {
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
    }


def test_citation_coverage_counts_claims_with_at_least_one_citation() -> None:
    claims = [
        {"citation_ids": ["ev-1"]},
        {"citation_ids": []},
        {"citation_ids": ["ev-2", "ev-3"]},
    ]

    assert citation_coverage(claims) == pytest.approx(2 / 3)
    assert citation_coverage([]) == 0.0


def test_citation_correctness_requires_cited_evidence_for_each_claim() -> None:
    claims = [
        {"citation_ids": ["ev-1", "ev-2"]},
        {"citation_ids": ["ev-missing"]},
        {"citation_ids": []},
    ]
    evidence_ids = {"ev-1", "ev-2", "ev-3"}

    assert citation_correctness(claims, evidence_ids) == pytest.approx(1 / 3)
    assert citation_correctness([], evidence_ids) == 0.0


def test_task_success_rate_counts_only_fully_successful_scenarios() -> None:
    scenarios = [
        {"completed": True, "citations_valid": True, "expected_items_returned": True},
        {"completed": True, "citations_valid": False, "expected_items_returned": True},
        {"completed": True, "citations_valid": True, "expected_items_returned": True},
    ]

    assert task_success_rate(scenarios) == pytest.approx(2 / 3)
    assert task_success_rate([]) == 0.0


def test_percentile_latency_returns_p50_and_p95_and_handles_empty() -> None:
    latencies_ms = [100.0, 200.0, 300.0, 400.0, 500.0]

    percentiles = percentile_latency(latencies_ms)

    assert percentiles == {"p50": 300.0, "p95": 480.0}
    assert percentile_latency([]) == {"p50": 0.0, "p95": 0.0}


def test_total_tokens_aggregates_prompt_and_completion_tokens() -> None:
    from bidscope.llm.types import ModelUsage

    usage = [
        ModelUsage(
            model="fake",
            prompt_tokens=100,
            completion_tokens=25,
            latency_ms=1.0,
        ),
        ModelUsage(
            model="fake",
            prompt_tokens=40,
            completion_tokens=10,
            latency_ms=1.0,
        ),
    ]

    assert total_tokens(usage) == {"prompt": 140, "completion": 35, "total": 175}
    assert total_tokens([]) == {"prompt": 0, "completion": 0, "total": 0}


def test_cny_cost_converts_tokens_using_pricing_snapshot() -> None:
    usage = {"prompt": 1_000, "completion": 500}
    pricing_cny_per_million = {"prompt": 1.0, "completion": 2.0}

    assert cny_cost(usage, pricing_cny_per_million) == pytest.approx(0.002)
    assert cny_cost({"prompt": 0, "completion": 0}, pricing_cny_per_million) == 0.0
