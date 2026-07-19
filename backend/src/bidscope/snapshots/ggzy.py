"""GGZY (全国公共资源交易平台) curated-excerpt snapshot adapter.

Parses a source-shaped fixture that imitates the structure of a GGZY tender
detail page: a ``div.title h2`` heading plus a ``ul.info`` list whose
``li.info-item`` rows pair a ``span.label`` with a ``span.value``. This
structure differs from CCGP, so the selectors are intentionally separate —
each source owns one narrow, testable parsing rule rather than sharing a
generic guesser.

Only fields observed during source research are extracted; unknown labels are
ignored and missing values stay ``None``.
"""

from __future__ import annotations

from pathlib import Path

from bidscope.domain.enums import SourceName
from bidscope.domain.notices import NormalizedNotice
from bidscope.snapshots import _parse
from bidscope.snapshots.adapters import InspectionResult, inspect_bundle
from selectolax.parser import HTMLParser

_TITLE_SELECTOR = "div.title h2"
_ROW_SELECTOR = "ul.info li.info-item"
_LABEL_SELECTOR = "span.label"
_VALUE_SELECTOR = "span.value"


class GgzySnapshotAdapter:
    """Turn a curated GGZY excerpt bundle into validated NormalizedNotices."""

    source = SourceName.GGZY
    parser_version = "ggzy-v1"

    def parse(self, bundle: Path) -> list[NormalizedNotice]:
        inspection = inspect_bundle(bundle)
        self._require_valid_inspection(bundle, inspection)

        html = (bundle / "detail.html").read_text(encoding="utf-8")
        tree = HTMLParser(html)

        title = self._extract_title(tree)
        if not title:
            raise _parse.ParseDrift(
                "GGZY fixture is missing the required title element",
                path="detail.html:div.title h2",
                detail="title element not found",
            )

        fields: dict[str, str | None] = {"title": title}
        for row in tree.css(_ROW_SELECTOR):
            label_node = row.css_first(_LABEL_SELECTOR)
            value_node = row.css_first(_VALUE_SELECTOR)
            if label_node is None or value_node is None:
                continue
            label = _parse.normalize_whitespace(label_node.text(separator=" ", strip=True))
            value = _parse.normalize_whitespace(value_node.text(separator=" ", strip=True))
            if not label:
                continue
            field = _parse._map_field(label)
            if field is not None and value:
                fields[field] = value

        manifest = _parse.load_manifest(bundle)
        notice = _parse.build_notice(
            source=self.source,
            capture_kind=manifest.capture_kind,
            parser_version=self.parser_version,
            source_url=manifest.source_urls[0],
            fields=fields,
        )
        return [notice]

    def load_expected(self, bundle: Path) -> list[dict[str, object]]:
        return _parse.load_expected(bundle)

    @staticmethod
    def _extract_title(tree: HTMLParser) -> str | None:
        node = tree.css_first(_TITLE_SELECTOR)
        if node is None:
            return None
        return _parse.normalize_whitespace(node.text(separator=" ", strip=True))

    @staticmethod
    def _require_valid_inspection(bundle: Path, inspection: InspectionResult) -> None:
        if inspection.valid:
            return
        first = inspection.errors[0]
        raise _parse.ParseDrift(
            f"GGZY bundle failed integrity inspection: {first.code}",
            path="manifest.json",
            detail=first.message,
        )
