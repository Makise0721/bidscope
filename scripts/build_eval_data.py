"""Build the committed, deterministic BidScope evaluation datasets.

This script is intentionally explicit: it only writes the five JSONL datasets
and the synthetic corpus after validating every record in memory. It also mirrors
the exact resulting bytes into the installed package resource tree. The runner
loads these committed files and never calls this builder implicitly.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend" / "src"))

from bidscope.clock import FixedClock  # noqa: E402
from bidscope.domain.intents import SearchIntent  # noqa: E402
from bidscope.evaluation.datasets import (  # noqa: E402
    DATASET_PATHS,
    validate_generated_bundle,
)
from bidscope.llm.fake import FakeIntentModel  # noqa: E402
from bidscope.retrieval.deduplication import DuplicateDecision  # noqa: E402

FIXED_NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
REGIONS = ("四川", "重庆", "北京", "上海", "广东", "浙江")
TOPICS = ("智算中心", "服务器", "存储", "网络", "医疗", "教育")


def _url(kind: str, index: int) -> str:
    return f"https://example.invalid/eval/{kind}/{index:03d}"


def build_corpus() -> list[dict[str, Any]]:
    """Create 120 synthetic notices with stable content and identifiers."""
    records: list[dict[str, Any]] = []
    for index in range(120):
        number = index + 1
        region = REGIONS[index % len(REGIONS)]
        topic = TOPICS[index % len(TOPICS)]
        budget = (index + 1) * 100_000
        records.append(
            {
                "id": f"eval-notice-{number:03d}",
                "source": "synthetic_demo",
                "external_id": f"eval-notice-{number:03d}",
                "canonical_url": _url("notice", number),
                "title": f"{region}{topic}建设项目第{number}批采购公告",
                "region": region,
                "purchaser": f"{region}公共资源交易中心",
                "budget_minor_units": budget,
                "deadline": f"2026-08-{number % 28 + 1:02d}T17:00:00+00:00",
                "project_number": f"EVAL-{number:04d}",
                "content_hash": f"eval-content-{number:04d}",
                "content": (
                    f"合成演示数据：{region}{topic}项目，预算{budget}分，"
                    f"用于离线评估，不代表真实招标公告。"
                ),
            }
        )
    return records


def _intent_expected(request: str) -> dict[str, Any]:
    model = FakeIntentModel()
    try:
        intent = asyncio.run(model.parse(request, FixedClock(FIXED_NOW)))
    except ValueError as error:
        return {"error": type(error).__name__, "message": str(error)}
    if not isinstance(intent, SearchIntent):  # pragma: no cover - defensive
        raise TypeError("fake intent model returned an unexpected value")
    return {
        "topics": intent.topics,
        "expanded_terms": intent.expanded_terms,
        "regions": intent.regions,
        "published_from": intent.published_from.isoformat()
        if intent.published_from
        else None,
        "published_to": intent.published_to.isoformat() if intent.published_to else None,
        "min_budget_minor_units": intent.min_budget.minor_units if intent.min_budget else None,
        "max_budget_minor_units": intent.max_budget.minor_units if intent.max_budget else None,
        "schedule_cron": intent.schedule.cron_expression if intent.schedule else None,
        "schedule_timezone": intent.schedule.timezone if intent.schedule else None,
    }


def build_intent_cases() -> list[dict[str, Any]]:
    """Create 120 fixed Chinese intent cases, including ambiguity/error cases."""
    templates = (
        "查找{region}{topic}项目，预算{budget}万元以上，近{days}天",
        "请关注{region}的{topic}采购公告",
        "每周一9点提醒我{region}{topic}，预算{budget}万元以上",
        "查找「{topic}、服务器」相关项目，预算{budget}万元以下",
        "最近{days}天有哪些{topic}招标信息？",
        "不限地区的{topic}项目",
    )
    records: list[dict[str, Any]] = []
    for index in range(120):
        number = index + 1
        if index == 114:
            request = ""
            case_type = "error_empty_request"
        elif index == 115:
            request = "   "
            case_type = "error_whitespace_request"
        elif index == 116:
            request = "不明确的项目需求，请帮我看看"
            case_type = "ambiguity_missing_filters"
        else:
            template = templates[index % len(templates)]
            request = template.format(
                region=REGIONS[index % len(REGIONS)],
                topic=TOPICS[index % len(TOPICS)],
                budget=(index % 9 + 1) * 10,
                days=index % 14 + 1,
            )
            case_type = "template"
        expected = _intent_expected(request)
        records.append(
            {
                "id": f"eval-intent-{number:03d}",
                "source": "synthetic_demo",
                "source_url": _url("intent", number),
                "request": request,
                "expected": expected,
                "metadata": {"case_type": case_type, "clock": FIXED_NOW.isoformat()},
            }
        )
    return records


def build_retrieval_cases(corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create 30 retrieval tasks whose relevance labels point into the corpus."""
    records: list[dict[str, Any]] = []
    for index in range(30):
        notice = corpus[index * 3]
        records.append(
            {
                "id": f"eval-retrieval-{index + 1:03d}",
                "source": "synthetic_demo",
                "source_url": _url("retrieval", index + 1),
                "query": notice["title"].replace("公告", ""),
                "filters": {"regions": [notice["region"]]},
                "relevant_ids": [notice["id"]],
                "expected_top_k": 10,
            }
        )
    return records


def _notice_view(record: dict[str, Any], *, suffix: str = "") -> dict[str, Any]:
    result = {
        "source": "synthetic_demo",
        "external_id": f"{record['id']}{suffix}",
        "canonical_url": record["canonical_url"],
        "project_number": record["project_number"],
        "content_hash": record["content_hash"],
        "title": record["title"],
        "purchaser": record["purchaser"],
        "region": record["region"],
        "budget_minor_units": record["budget_minor_units"],
        "budget_currency": "CNY",
        "deadline": record["deadline"],
        "procurement_scope": "标准采购范围",
        "cancellation": False,
        "claim_supporting_texts": (record["content"],),
    }
    return result


def build_dedup_cases(corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create 120 exact, distinct, and ambiguous labeled notice pairs."""
    records: list[dict[str, Any]] = []
    for index in range(120):
        left = _notice_view(corpus[index])
        right = dict(left)
        if index < 40:
            right["external_id"] = f"{left['external_id']}-channel-b"
            expected = DuplicateDecision.EXACT
            case_type = "exact_shared_content"
        elif index < 80:
            right["external_id"] = f"{left['external_id']}-distinct"
            right["project_number"] = f"DISTINCT-{index:04d}"
            right["content_hash"] = f"distinct-content-{index:04d}"
            right["canonical_url"] = _url("distinct-notice", index + 1)
            right["purchaser"] = "另一合成采购人"
            right["region"] = REGIONS[(index + 1) % len(REGIONS)]
            right["budget_minor_units"] = int(left["budget_minor_units"]) + 1_000_000
            expected = DuplicateDecision.DISTINCT
            case_type = "distinct_conflicting_fields"
        else:
            right["external_id"] = f"{left['external_id']}-ambiguous"
            right["project_number"] = None
            right["content_hash"] = f"ambiguous-content-{index:04d}"
            right["canonical_url"] = _url("other-notice", index + 1)
            expected = DuplicateDecision.AMBIGUOUS
            case_type = "ambiguous_weak_evidence"
        records.append(
            {
                "id": f"eval-dedup-{index + 1:03d}",
                "source": "synthetic_demo",
                "source_url": _url("dedup", index + 1),
                "left": left,
                "right": right,
                "expected_decision": expected,
                "metadata": {"case_type": case_type},
            }
        )
    return records


def build_claim_cases() -> list[dict[str, Any]]:
    """Create 60 report-claim cases with citations scoped to case evidence."""
    records: list[dict[str, Any]] = []
    for index in range(60):
        number = index + 1
        evidence_id = f"eval-evidence-{number:03d}"
        records.append(
            {
                "id": f"eval-claim-{number:03d}",
                "source": "synthetic_demo",
                "source_url": _url("claim", number),
                "notice_id": f"eval-notice-{number:03d}",
                "claims": [
                    {
                        "text": f"合成证据{number}支持该项目字段。",
                        "citation_ids": [evidence_id],
                    }
                ],
                "evidence_ids": [evidence_id],
                "expected_supported": True,
            }
        )
    return records


def build_e2e_cases(corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create 30 fixed end-to-end scenarios with deterministic accounting."""
    records: list[dict[str, Any]] = []
    for index in range(30):
        notice = corpus[index]
        records.append(
            {
                "id": f"eval-e2e-{index + 1:03d}",
                "source": "synthetic_demo",
                "source_url": _url("e2e", index + 1),
                "request": f"查找{notice['region']}{TOPICS[index % len(TOPICS)]}项目",
                "expected_notice_ids": [notice["id"]],
                "completed": True,
                "citations_valid": True,
                "expected_items_returned": True,
                "latency_ms": 12.0 + (index % 5),
                "usage": {"prompt": 40 + index, "completion": 12},
            }
        )
    return records


def _validate_generated(
    corpus: list[dict[str, Any]], datasets: dict[str, list[dict[str, Any]]]
) -> None:
    validate_generated_bundle(corpus, datasets)


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Write deterministic UTF-8 JSONL with explicit LF bytes on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records
    ).encode("utf-8")
    path.write_bytes(payload)


def _sync_package_resources() -> None:
    """Copy approved checkout bytes into the wheel package resource tree."""
    package_root = ROOT / "backend" / "src" / "bidscope" / "evaluation"
    sources = {
        ROOT / "eval" / "corpus" / "synthetic-notices-v1.jsonl": package_root
        / "corpus"
        / "synthetic-notices-v1.jsonl",
        **{
            ROOT / "eval" / "data" / f"{name}.jsonl": package_root / "data" / f"{name}.jsonl"
            for name in DATASET_PATHS
        },
    }
    for source, destination in sources.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def build_all() -> dict[str, int]:
    """Build and validate all committed datasets, returning record counts."""
    corpus = build_corpus()
    datasets = {
        "intent-v1": build_intent_cases(),
        "retrieval-v1": build_retrieval_cases(corpus),
        "dedup-v1": build_dedup_cases(corpus),
        "claims-v1": build_claim_cases(),
        "e2e-v1": build_e2e_cases(corpus),
    }
    _validate_generated(corpus, datasets)
    _write_jsonl(ROOT / "eval" / "corpus" / "synthetic-notices-v1.jsonl", corpus)
    for name, records in datasets.items():
        _write_jsonl(ROOT / "eval" / "data" / f"{name}.jsonl", records)
    _sync_package_resources()
    return {"corpus": len(corpus), **{name: len(records) for name, records in datasets.items()}}


if __name__ == "__main__":
    counts = build_all()
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
