"""CCGP (中国政府采购网) curated-excerpt snapshot adapter.

Parses a source-shaped fixture that imitates the structure of a CCGP central
open-tender detail page: a ``h2.title`` heading plus a ``table.table`` whose
rows pair a ``td.title`` label with a ``td.text`` value. Only the fields
observed during source research are extracted; everything else stays ``None``
or lands in ``raw_fields``.

This is intentionally a narrow, source-specific parser. It is not a generic
CSS guesser: if the page structure drifts, it raises :class:`ParseDrift`
rather than silently producing wrong fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bidscope.domain.enums import SourceName
from bidscope.domain.notices import NormalizedNotice
from bidscope.domain.snapshots import SnapshotManifest
from bidscope.snapshots import _parse
from bidscope.snapshots.adapters import InspectionResult, inspect_bundle
from selectolax.parser import HTMLParser

#: CSS selectors that pin the CCGP page structure. Kept in one place so a
#: structure change fails fast and obviously.
_TITLE_SELECTOR = "h2.title"
_ROW_SELECTOR = "table.table tr"
_LABEL_SELECTOR = "td.title"
_VALUE_SELECTOR = "td.text"


class CcgpSnapshotAdapter:
    """Turn a curated CCGP excerpt bundle into validated NormalizedNotices."""

    source = SourceName.CCGP
    parser_version = "ccgp-v1"

    def parse(self, bundle: Path) -> list[NormalizedNotice]:
        inspection = inspect_bundle(bundle)
        self._require_valid_inspection(bundle, inspection)
        # Reuse the manifest already parsed during integrity inspection instead
        # of re-reading manifest.json a second time.
        manifest = inspection.manifest
        assert manifest is not None  # valid inspection guarantees a parsed manifest

        if manifest.capture_kind.value == "raw_response":
            return self._parse_raw_response(bundle, manifest)

        html = (bundle / "detail.html").read_text(encoding="utf-8")
        tree = HTMLParser(html)

        title = self._extract_title(tree)
        if not title:
            raise _parse.ParseDrift(
                "CCGP fixture is missing the required title element",
                path="detail.html:h2.title",
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

        notice = _parse.build_notice(
            source=self.source,
            capture_kind=manifest.capture_kind,
            parser_version=self.parser_version,
            source_url=manifest.source_urls[0],
            fields=fields,
        )
        return [notice]

    def _parse_raw_response(
        self, bundle: Path, manifest: SnapshotManifest
    ) -> list[NormalizedNotice]:
        """Parse the approved JSON response contract used by live ingestion."""
        try:
            payload = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _parse.ParseDrift(
                "CCGP authorized response is not valid JSON",
                path="response.json",
                detail="invalid_json",
            ) from error
        if manifest.acquisition_metadata is None:
            raise _parse.ParseDrift(
                "CCGP authorized response has no response contract metadata",
                path="manifest.json:acquisition_metadata",
                detail="missing_response_contract",
            )
        items_field = manifest.acquisition_metadata.response_items_field
        if not isinstance(payload, dict) or not isinstance(payload.get(items_field), list):
            raise _parse.ParseDrift(
                "CCGP authorized response is missing the items list",
                path=f"response.json:{items_field}",
                detail="items must be a list",
            )

        notices: list[NormalizedNotice] = []
        field_map = manifest.acquisition_metadata.notice_field_map
        for index, item in enumerate(payload[items_field]):
            if not isinstance(item, dict):
                raise _parse.ParseDrift(
                    "CCGP authorized response item is not an object",
                    path=f"response.json:items[{index}]",
                    detail="item must be an object",
                )
            external_id = self._mapped_text(item, "external_id", field_map)
            if not external_id:
                raise _parse.ParseDrift(
                    "CCGP authorized response item has no identifier",
                    path=f"response.json:items[{index}].external_id",
                    detail="identifier is required",
                )
            fields = {
                "external_id": external_id,
                "title": self._mapped_text(item, "title", field_map),
                "purchaser": self._mapped_text(item, "purchaser", field_map),
                "region": self._mapped_text(item, "region", field_map),
                "publish_time": self._mapped_text(item, "publish_time", field_map),
                "deadline": self._mapped_text(item, "deadline", field_map),
                "budget": self._mapped_text(item, "budget", field_map),
            }
            notices.append(
                _parse.build_notice(
                    source=self.source,
                    capture_kind=manifest.capture_kind,
                    parser_version=manifest.parser_version,
                    source_url=manifest.source_urls[0],
                    fields=fields,
                )
            )
        return notices

    @staticmethod
    def _mapped_text(
        item: dict[str, Any], field: str, field_map: dict[str, str]
    ) -> str | None:
        key = field_map.get(field, field)
        value = item.get(key)
        if isinstance(value, str):
            return _parse.normalize_whitespace(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return None

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
            f"CCGP bundle failed integrity inspection: {first.code}",
            path="manifest.json",
            detail=first.message,
        )
