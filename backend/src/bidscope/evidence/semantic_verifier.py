"""Semantic Citation Contract (SEMANTIC_CITATION_CONTRACT.md) contract types.

This module pins the *input/output boundary* of the semantic verifier: the
three-way support status, the immutable verification record every verifier
must return, and the :class:`SemanticClaimVerifier` protocol the graph depends
on. It defines no LLM logic — the deterministic fake and the DeepSeek adapter
both implement the protocol.

Contract §2 input boundary: a verifier may only use the evidence sequence
passed to :meth:`SemanticClaimVerifier.verify` (the claim's own citation set
that already passed deterministic ``validate_claim``). It must never read other
text of the same notice, call an external knowledge base, or use web
commonsense.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from bidscope.domain.enums import ClaimSupportStatus
from bidscope.domain.notices import NoticeEvidence
from bidscope.domain.reports import ReportClaim


@dataclass(frozen=True, slots=True)
class ClaimSupportVerification:
    """
    Semantic Citation Contract 标准输出结构 (契约 §4)
    所有语义校验实现必须返回该不可变结构体，用于审计、复核、报告聚合。
    """

    status: ClaimSupportStatus
    rationale: str
    evidence_ids_used: tuple[str, ...]
    conflict_evidence_ids: tuple[str, ...]
    verifier_version: str

    def __post_init__(self) -> None:
        """内置基础合法性校验（契约强制约束，运行时防护）"""
        if not self.rationale.strip():
            raise ValueError("rationale cannot be empty or whitespace")
        if not self.verifier_version.strip():
            raise ValueError("verifier_version cannot be empty or whitespace")
        # 契约 §4: 非 UNSUPPORTED 时 conflict_evidence_ids 必须为空列表。
        if self.status != ClaimSupportStatus.UNSUPPORTED and self.conflict_evidence_ids:
            raise ValueError(
                "conflict_evidence_ids must be empty unless status is UNSUPPORTED"
            )
        # 冲突证据必须先被判定为"已使用"（契约 §4 语义互斥）。
        if not set(self.conflict_evidence_ids).issubset(set(self.evidence_ids_used)):
            raise ValueError(
                "conflict_evidence_ids must be a subset of evidence_ids_used"
            )


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    """A semantic verification pinned to one claim of one report item.

    ``notice_id`` is the report item's ``notice_id`` and ``claim_index`` is the
    claim's ordinal inside that item, so the persistence layer can attach the
    verdict to exactly the right
    :class:`~bidscope.domain.reports.ReportClaim` row.
    """

    notice_id: str
    claim_index: int
    verification: ClaimSupportVerification


class SemanticClaimVerifier(Protocol):
    """
    语义校验器标准协议（稳定插座，契约 §2/§4）
    上层工作流、Report聚合层仅依赖此Protocol，不依赖具体LLM实现
    更换模型/提示词策略、切换本地模型/云端API，无需改动Graph核心逻辑

    协议方法是 async 的（与 :mod:`bidscope.llm.ports` 一致），因为真实
    provider 需要网络调用；确定性 fake 实现同样保持该签名以便无缝替换。
    """

    async def verify(
        self,
        claim: ReportClaim,
        evidence: Sequence[NoticeEvidence],
        *,
        evidence_ids: Sequence[str] | None = None,
    ) -> ClaimSupportVerification:
        """
        Args:
            claim: 待校验断言（已经通过底层 validate_claim 元数据校验）
            evidence: Claim 引用的全部证据集合
                契约约束：Verifier **仅允许使用该序列内证据**
                禁止加载外部上下文、外部知识库、未引用公告片段
            evidence_ids: 与 evidence 等长平行的引用 ID 序列（即 claim 的
                citation_ids 顺序），用于在判定记录中标记参与/冲突的证据。
        """
        ...


__all__ = [
    "ClaimSupportVerification",
    "ClaimSupportStatus",
    "ClaimVerification",
    "SemanticClaimVerifier",
]
