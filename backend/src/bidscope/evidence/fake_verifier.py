"""Deterministic, fully offline semantic verifier.

Implements the :class:`~bidscope.evidence.semantic_verifier.SemanticClaimVerifier`
protocol with pure regex/string logic — no randomness, no network, no API key
(aligning with :mod:`bidscope.llm.fake`). It exists so the graph and the
contract test suite run offline and reproducibly; a configured deployment can
swap in :mod:`bidscope.evidence.deepseek_verifier` without touching graph code.

Judgment rules mirror the Semantic Citation Contract §3 priority:

1. An evidence span whose money amount is explicitly different from every
   amount the claim asserts is an UNSUPPORTED conflict (contract §7, case 2).
2. Otherwise, when the claim's amounts are all backed by matching evidence
   amounts and the claim asserts no certainty qualifier the evidence lacks,
   the claim is SUPPORTED (contract §7, case 1).
3. Everything else — unrelated evidence, missing key facts, or a certainty
   qualifier the evidence cannot confirm — is UNCERTAIN (contract §7, cases 3
   and 4).

Only the claim and the explicitly supplied evidence are inspected; the verifier
never reads other text of the notice or external knowledge.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from bidscope.domain.enums import ClaimSupportStatus
from bidscope.domain.notices import NoticeEvidence
from bidscope.domain.reports import ReportClaim
from bidscope.evidence.semantic_verifier import (
    ClaimSupportVerification,
)

VERIFIER_VERSION = "fake-deterministic-v1"

#: Amount patterns: a number with an explicit 万/亿 unit or an explicit 元,
#: so dates and other bare digit groups are never mistaken for money.
_AMOUNT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(万|亿)\s*元?|(\d+(?:\.\d+)?)\s*元")
_WAN_PER_YI = 10_000.0
_PUNCTUATION = re.compile(r"[\s，。、：:；;（）()【】\[\]「」『』“”\"'—\-–…·]+")

#: Qualifiers that assert a degree of certainty the evidence alone may not
#: justify (contract §7, case 4: “确定” without “暂估金额/以批复为准” context).
_CERTAINTY_QUALIFIERS = ("确定", "已确定", "最终", "已批准")


def _amounts_wan(text: str) -> tuple[float, ...]:
    """Extract money amounts from ``text``, normalized to 万元."""
    amounts: list[float] = []
    for match in _AMOUNT_PATTERN.finditer(text):
        number = match.group(1) or match.group(3)
        unit = match.group(2)
        value = float(number)
        if unit == "亿":
            value *= _WAN_PER_YI
        amounts.append(value)
    return tuple(amounts)


def _normalized(text: str) -> str:
    """Strip whitespace and punctuation for substring-level comparison."""
    return _PUNCTUATION.sub("", text)


class FakeSemanticVerifier:
    """Deterministic semantic verifier (offline, reproducible)."""

    async def verify(
        self,
        claim: ReportClaim,
        evidence: Sequence[NoticeEvidence],
        *,
        evidence_ids: Sequence[str] | None = None,
    ) -> ClaimSupportVerification:
        ids = list(evidence_ids) if evidence_ids is not None else [
            f"ev-{index}" for index in range(len(evidence))
        ]
        if len(ids) != len(evidence):
            raise ValueError("evidence_ids must parallel the evidence sequence")

        claim_amounts = _amounts_wan(claim.text)
        claim_normalized = _normalized(claim.text)
        evidence_normalized = _normalized("\n".join(ev.text for ev in evidence))

        conflict_ids: list[str] = []
        for index, ev in enumerate(evidence):
            ev_amounts = _amounts_wan(ev.text)
            # An evidence span with an explicit amount that contradicts every
            # claim amount is a clear key-fact conflict (contract §7, case 2).
            if ev_amounts and claim_amounts and not any(
                amount in ev_amounts for amount in claim_amounts
            ):
                conflict_ids.append(ids[index])

        if conflict_ids:
            used = tuple(ids)
            return ClaimSupportVerification(
                status=ClaimSupportStatus.UNSUPPORTED,
                rationale=(
                    "证据中记载的金额与 Claim 声明的金额不一致，构成明确的关键事实冲突。"
                ),
                evidence_ids_used=used,
                conflict_evidence_ids=tuple(conflict_ids),
                verifier_version=VERIFIER_VERSION,
            )

        if claim_amounts:
            evidence_amounts = [
                amount for ev in evidence for amount in _amounts_wan(ev.text)
            ]
            if not any(claim_amount in evidence_amounts for claim_amount in claim_amounts):
                # No evidence carries a matching amount: the key fact is absent
                # (contract §7, case 3).
                return ClaimSupportVerification(
                    status=ClaimSupportStatus.UNCERTAIN,
                    rationale="证据中没有与 Claim 金额一致的关键事实，且不存在明确冲突。",
                    evidence_ids_used=tuple(ids),
                    conflict_evidence_ids=(),
                    verifier_version=VERIFIER_VERSION,
                )
            # Amounts match. A certainty qualifier the evidence cannot confirm
            # leaves the claim unverifiable (contract §7, case 4).
            qualifier = next(
                (q for q in _CERTAINTY_QUALIFIERS if q in claim.text), None
            )
            if qualifier is not None and qualifier not in evidence_normalized:
                return ClaimSupportVerification(
                    status=ClaimSupportStatus.UNCERTAIN,
                    rationale=(
                        f"金额与证据一致，但 Claim 的“{qualifier}”限定无法由所给证据确认；"
                        "未提供“暂估金额/以批复为准”等上下文时不得判定为确定。"
                    ),
                    evidence_ids_used=tuple(ids),
                    conflict_evidence_ids=(),
                    verifier_version=VERIFIER_VERSION,
                )
            return ClaimSupportVerification(
                status=ClaimSupportStatus.SUPPORTED,
                rationale="证据明确记载了与 Claim 一致的金额。",
                evidence_ids_used=tuple(ids),
                conflict_evidence_ids=(),
                verifier_version=VERIFIER_VERSION,
            )

        # No amounts on either side: fall back to substring-level support.
        if claim_normalized and claim_normalized in evidence_normalized:
            return ClaimSupportVerification(
                status=ClaimSupportStatus.SUPPORTED,
                rationale="证据文本包含 Claim 的表述（同义转述/直接引用）。",
                evidence_ids_used=tuple(ids),
                conflict_evidence_ids=(),
                verifier_version=VERIFIER_VERSION,
            )
        return ClaimSupportVerification(
            status=ClaimSupportStatus.UNCERTAIN,
            rationale="证据与 Claim 之间没有可核对的关键事实，也没有明确冲突。",
            evidence_ids_used=tuple(ids),
            conflict_evidence_ids=(),
            verifier_version=VERIFIER_VERSION,
        )


__all__ = ["FakeSemanticVerifier", "VERIFIER_VERSION"]
