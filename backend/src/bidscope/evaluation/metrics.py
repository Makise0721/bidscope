"""Pure quality, latency, and cost metrics used by the evaluation runner."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


def _aligned(expected: Sequence[Any], predicted: Sequence[Any]) -> list[tuple[Any, Any]]:
    """Pair sequences and represent an omitted prediction as ``None``."""
    size = max(len(expected), len(predicted))
    return [
        (expected[index] if index < len(expected) else None,
         predicted[index] if index < len(predicted) else None)
        for index in range(size)
    ]


def _label(value: Any) -> str:
    """Convert scalar or structured field values to a stable comparable label."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def field_exact_match(
    expected: Sequence[Mapping[str, Any]], predicted: Sequence[Mapping[str, Any]]
) -> dict[str, float]:
    """Return exact-match rates for each field appearing in either sequence."""
    pairs = _aligned(expected, predicted)
    fields = sorted({field for item in expected for field in item} | {
        field for item in predicted for field in item
    })
    if not pairs:
        return {}
    return {
        field: sum(
            left.get(field) == right.get(field)
            for left, right in pairs
            if isinstance(left, Mapping) and isinstance(right, Mapping)
        ) / len(pairs)
        for field in fields
    }


def field_macro_f1(
    expected: Sequence[Mapping[str, Any]], predicted: Sequence[Mapping[str, Any]]
) -> dict[str, float]:
    """Compute macro-F1 over the labels observed for each field."""
    pairs = _aligned(expected, predicted)
    fields = sorted({field for item in expected for field in item} | {
        field for item in predicted for field in item
    })
    if not pairs:
        return {}
    scores: dict[str, float] = {}
    for field in fields:
        labels = {
            _label(item.get(field))
            for item in (*expected, *predicted)
            if isinstance(item, Mapping)
        }
        per_label: list[float] = []
        for label in labels:
            true_positive = sum(
                isinstance(left, Mapping)
                and isinstance(right, Mapping)
                and _label(left.get(field)) == label
                and _label(right.get(field)) == label
                for left, right in pairs
            )
            false_positive = sum(
                isinstance(right, Mapping)
                and _label(right.get(field)) == label
                and (not isinstance(left, Mapping) or _label(left.get(field)) != label)
                for left, right in pairs
            )
            false_negative = sum(
                isinstance(left, Mapping)
                and _label(left.get(field)) == label
                and (not isinstance(right, Mapping) or _label(right.get(field)) != label)
                for left, right in pairs
            )
            denominator = 2 * true_positive + false_positive + false_negative
            per_label.append(
                2 * true_positive / denominator if denominator else 0.0
            )
        scores[field] = sum(per_label) / len(per_label) if per_label else 0.0
    return scores


def recall_at_k(relevant: set[str], ranked: Sequence[str], *, k: int = 10) -> float:
    """Return the fraction of relevant IDs found in the first ``k`` results."""
    if not relevant or k <= 0:
        return 0.0
    return len(relevant.intersection(ranked[:k])) / len(relevant)


def ndcg_at_k(relevant: set[str], ranked: Sequence[str], *, k: int = 10) -> float:
    """Return binary-relevance normalized discounted cumulative gain."""
    if not relevant or k <= 0:
        return 0.0
    dcg = sum(
        1.0 / math.log2(index + 2)
        for index, item_id in enumerate(ranked[:k])
        if item_id in relevant
    )
    ideal_count = min(len(relevant), k)
    ideal = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
    return dcg / ideal if ideal else 0.0


def binary_classification_metrics(
    actual: Sequence[bool], predicted: Sequence[bool]
) -> dict[str, float]:
    """Return binary precision, recall, and F1 with safe zero denominators."""
    pairs = _aligned(actual, predicted)
    true_positive = sum(left and right for left, right in pairs)
    false_positive = sum((not left) and right for left, right in pairs)
    false_negative = sum(left and (not right) for left, right in pairs)
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def citation_coverage(claims: Sequence[Mapping[str, Any]]) -> float:
    """Return the share of claims with at least one citation."""
    if not claims:
        return 0.0
    return sum(bool(claim.get("citation_ids")) for claim in claims) / len(claims)


def citation_correctness(
    claims: Sequence[Mapping[str, Any]], evidence_ids: set[str]
) -> float:
    """Return the share of claims whose citations all resolve to evidence."""
    if not claims:
        return 0.0
    return sum(
        bool(claim.get("citation_ids"))
        and set(claim.get("citation_ids", ())).issubset(evidence_ids)
        for claim in claims
    ) / len(claims)


def task_success_rate(scenarios: Sequence[Mapping[str, Any]]) -> float:
    """Return the share of scenarios satisfying every required success flag."""
    if not scenarios:
        return 0.0
    required = ("completed", "citations_valid", "expected_items_returned")
    return sum(all(bool(scenario.get(key)) for key in required) for scenario in scenarios) / len(
        scenarios
    )


def percentile_latency(latencies_ms: Sequence[float]) -> dict[str, float]:
    """Return linearly interpolated P50 and P95 latency in milliseconds."""
    if not latencies_ms:
        return {"p50": 0.0, "p95": 0.0}
    values = sorted(float(value) for value in latencies_ms)

    def percentile(fraction: float) -> float:
        position = (len(values) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return values[lower]
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {"p50": percentile(0.5), "p95": percentile(0.95)}


def _usage_value(usage: Any, field: str) -> int:
    if isinstance(usage, Mapping):
        return int(usage.get(field, 0))
    return int(getattr(usage, field, 0))


def total_tokens(usages: Sequence[Any]) -> dict[str, int]:
    """Aggregate prompt, completion, and total tokens from usage receipts."""
    prompt = sum(_usage_value(usage, "prompt_tokens") for usage in usages)
    completion = sum(_usage_value(usage, "completion_tokens") for usage in usages)
    return {"prompt": prompt, "completion": completion, "total": prompt + completion}


def cny_cost(usage: Mapping[str, Any], pricing_cny_per_million: Mapping[str, float]) -> float:
    """Convert token counts to CNY using a pinned per-million-token snapshot."""
    prompt = float(usage.get("prompt", 0))
    completion = float(usage.get("completion", 0))
    return (
        prompt * float(pricing_cny_per_million.get("prompt", 0.0))
        + completion * float(pricing_cny_per_million.get("completion", 0.0))
    ) / 1_000_000


__all__ = [
    "binary_classification_metrics",
    "citation_correctness",
    "citation_coverage",
    "cny_cost",
    "field_exact_match",
    "field_macro_f1",
    "ndcg_at_k",
    "percentile_latency",
    "recall_at_k",
    "task_success_rate",
    "total_tokens",
]
