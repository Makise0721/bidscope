"""Explicit synthetic-demo snapshot adapter.

Unlike the official adapters, the demo adapter reads a *normalised* JSON
payload rather than imitating source HTML. This keeps the demonstration,
incremental-update, failure and end-to-end scenarios obviously synthetic.

Everyrecord is forced to ``source=synthetic_demo`` / ``capture_kind=synthetic_demo``;
the ``NormalizedNotice`` provenance validator is the second line of defence
that rejects any ``demo-`` prefix, ``example.invalid`` URL or source/capture
spoofing. We never call ``model_construct()`` to bypass it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.notices import NormalizedNotice
from bidscope.domain.snapshots import SnapshotManifest
from bidscope.snapshots import _parse
from bidscope.snapshots.adapters import InspectionResult, inspect_bundle
from pydantic import HttpUrl

_PAYLOAD_FILE = "notices.json"


class DemoSnapshotAdapter:
    """Turn a synthetic-demo bundle into validated NormalizedNotices."""

    source = SourceName.SYNTHETIC_DEMO
    capture_kind = CaptureKind.SYNTHETIC_DEMO
    parser_version = "demo-v1"

    def parse(self, bundle: Path) -> list[NormalizedNotice]:
        inspection = inspect_bundle(bundle)
        self._require_valid_inspection(bundle, inspection)
        # Reuse the manifest already parsed during integrity inspection instead
        # of re-reading manifest.json a second time.
        manifest = inspection.manifest
        assert manifest is not None  # valid inspection guarantees a parsed manifest

        payload = json.loads((bundle / _PAYLOAD_FILE).read_text(encoding="utf-8"))
        records = payload.get("notices")
        if not isinstance(records, list):
            raise _parse.ParseDrift(
                "demo payload must contain a 'notices' array",
                path=_PAYLOAD_FILE,
                detail="missing notices array",
            )

        notices: list[NormalizedNotice] = []
        for record in records:
            notices.append(self._parse_record(manifest, record))
        return notices

    def load_expected(self, bundle: Path) -> list[dict[str, object]]:
        return _parse.load_expected(bundle)

    def _parse_record(
        self, manifest: SnapshotManifest, record: dict[str, Any]
    ) -> NormalizedNotice:
        external_id = str(record.get("id", ""))
        url = str(record.get("url", ""))

        fields: dict[str, str | None] = {
            "external_id": external_id,
            "title": _parse.normalize_whitespace(record.get("title")),
            "purchaser": _parse.normalize_whitespace(record.get("purchaser")),
            "region": _parse.normalize_whitespace(record.get("region")),
            "publish_time": _parse.normalize_whitespace(record.get("publish_time")),
            "deadline": _parse.normalize_whitespace(record.get("deadline")),
            "budget": _parse.normalize_whitespace(record.get("budget")),
        }
        # The synthetic_channel exists to exercise cross-channel dedup, but it
        # never changes the source identity — it is metadata, not a field.
        extra_raw: dict[str, Any] = {}
        channel = record.get("synthetic_channel")
        if channel is not None:
            extra_raw["synthetic_channel"] = channel

        return _parse.build_notice(
            source=self.source,
            capture_kind=self.capture_kind,
            parser_version=manifest.parser_version,
            source_url=HttpUrl(url),
            fields=fields,
            extra_raw_fields=extra_raw,
        )

    @staticmethod
    def _require_valid_inspection(bundle: Path, inspection: InspectionResult) -> None:
        if inspection.valid:
            return
        first = inspection.errors[0]
        raise _parse.ParseDrift(
            f"demo bundle failed integrity inspection: {first.code}",
            path="manifest.json",
            detail=first.message,
        )
