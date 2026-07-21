"""Immutable evidence-span construction for verified opportunities.

The extractor turns a notice's source text and the snippets a verified
opportunity claims to quote into :class:`~bidscope.domain.notices.NoticeEvidence`
records with explicit character offsets and a SHA-256 span hash. Because the
whole pipeline is a pure function of the source text and the claimed snippets,
the same text always produces the same span, and the validator can later
confirm the span has not drifted.

P0 is snapshot-only: the extractor does not fetch anything. It only receives
the source text already bound to a verified opportunity.
"""

from __future__ import annotations

from bidscope.domain.notices import NoticeEvidence
from bidscope.evidence.validator import hash_span


class ExtractionError(Exception):
    """Raised when a claimed snippet cannot be located in the source text."""


def extract_evidence(
    notice_version_id: str,
    source_text: str,
    snippets: tuple[str, ...],
) -> tuple[NoticeEvidence, ...]:
    """Build an immutable evidence span per claimed ``snippet``.

    Each snippet is located in ``source_text``; its character offsets and a
    SHA-256 hash of the snippet text are stored on the returned
    :class:`~bidscope.domain.notices.NoticeEvidence`. Snippets are matched in
    order and may not overlap, which mirrors how a human-reviewed excerpt is
    prepared.

    Raises :class:`ExtractionError` if a snippet cannot be found at a non-
    overlapping position, signalling drift between a claim and its source.
    """
    spans: list[NoticeEvidence] = []
    cursor = 0
    for index, snippet in enumerate(snippets):
        if not snippet:
            raise ExtractionError(
                f"empty snippet for notice {notice_version_id} at index {index}"
            )
        start = source_text.find(snippet, cursor)
        if start == -1:
            raise ExtractionError(
                f"snippet {snippet!r} not found in notice {source_text!r} from offset {cursor}"
            )
        end = start + len(snippet)
        spans.append(NoticeEvidence(
            notice_version_id=notice_version_id,
            text=snippet,
            start=start,
            end=end,
            span_hash=hash_span(snippet),
        ))
        cursor = end
    return tuple(spans)


__all__ = ["ExtractionError", "extract_evidence"]
