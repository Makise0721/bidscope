"""Idempotent, version-preserving snapshot import.

The :class:`SnapshotImporter` turns a verified snapshot bundle into database
records (snapshot bundle, source notices, immutable notice versions,
evidence) while writing the original payload to object storage.

Import is bracketed by the transaction order the audited ingestion contract
requires:

1. :func:`inspect_bundle` runs *before* any write transaction — an invalid
   bundle fails immediately, producing no database rows and no objects.
2. Each notice is stored under a deterministic, content-addressed object key
   (``snapshots/{bundle_id}/{content_hash}``), so repeated writes of the same
   content are idempotent.
3. The snapshot bundle and a ``SnapshotImport`` record are created or reused.
4. The ``SourceNotice`` is keyed by ``(source, external_id)``.
5. A new ``NoticeVersion`` is appended only when the notice's content hash
   changes; unchanged content finds the existing version and stops.
6. Evidence rows are linked to the version that owns them.
7. The import is marked successful only after everything commits; any error
   rolls the transaction back.

Object storage does not participate in the PostgreSQL transaction. Because
every object key is derived from the notice's content hash, a write that
survives a later database rollback (or a repeat import) stores *identical*
bytes under the *same* key — so orphan objects can never corrupt a later
import's semantics. This ``content-addressed`` strategy is exercised by the
integration tests rather than relying on best-effort deletion.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from bidscope.audit import AuditContext, AuditEventType, AuditOutcome, record_audit_event
from bidscope.clock import Clock, SystemClock
from bidscope.delivery.objects import LocalObjectStore, ObjectStore
from bidscope.domain.enums import SourceName
from bidscope.observability import METRICS_REGISTRY
from bidscope.persistence.repositories import SnapshotRepository
from bidscope.persistence.unit_of_work import UnitOfWork
from bidscope.snapshots import _parse
from bidscope.snapshots.adapters import inspect_bundle
from bidscope.snapshots.ccgp import CcgpSnapshotAdapter
from bidscope.snapshots.demo import DemoSnapshotAdapter
from bidscope.snapshots.ggzy import GgzySnapshotAdapter

logger = logging.getLogger(__name__)


class BundleAdapter(Protocol):
    """The narrow adapter surface the importer relies on."""

    def parse(self, bundle: Path) -> list[Any]: ...


#: Per-source adapter registry. The importer selects an adapter from the
#: bundle's declared source, so a bundle is always parsed by its own rules.
ADAPTERS: dict[SourceName, BundleAdapter] = {
    SourceName.CCGP: CcgpSnapshotAdapter(),
    SourceName.GGZY: GgzySnapshotAdapter(),
    SourceName.SYNTHETIC_DEMO: DemoSnapshotAdapter(),
}


class SnapshotImportError(Exception):
    """Raised when a bundle cannot be imported (integrity or provenance)."""


def _content_hash(notice: Any) -> str:
    """Deterministic content hash for a :class:`NormalizedNotice`.

    Derived from the canonical JSON so that the same notice content always
    maps to the same hash regardless of process or import run.
    """
    data = notice.model_dump(mode="json")
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_bytes(notice: Any) -> bytes:
    """Canonical serialized form of a notice, used as the stored payload."""
    data = notice.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _payload_object_key(bundle_id: str, content_hash: str) -> str:
    """Content-addressed object key for a notice payload."""
    return f"snapshots/{bundle_id}/{content_hash}"


class SnapshotImporter:
    """Import snapshot bundles idempotently and preserve notice versions."""

    def __init__(
        self,
        session_factory: Any,
        repository_factory: Any,
        object_store: ObjectStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.repository_factory = repository_factory
        self.object_store = object_store or LocalObjectStore(".data/objects")
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------- public

    async def import_bundle(self, bundle: Path) -> Any:
        """Import a snapshot bundle, returning its ``SnapshotImport`` record.

        Re-importing an identical bundle (same content) returns the existing
        successful import without creating duplicate rows.
        """
        bundle = bundle.resolve()

        # Step 1: integrity inspection BEFORE any write. An invalid bundle
        # raises here — no database rows, no objects are ever produced.
        inspection = inspect_bundle(bundle)
        if not inspection.valid:
            raise SnapshotImportError(
                f"bundle failed integrity inspection: "
                f"{[e.code for e in inspection.errors]}"
            )

        manifest = _parse.load_manifest(bundle)
        notices = self._parse(bundle)

        idempotency_key = self._derive_idempotency_key(manifest.bundle_id, notices)

        async with UnitOfWork(self.session_factory) as uow:
            if uow.session is None:
                raise RuntimeError("snapshot import session was not initialized")
            session = uow.session
            repository = self.repository_factory(session)

            # Idempotent short-circuit: a successful import for this exact
            # bundle content already exists, so return it unchanged.
            existing = await repository.find_import(idempotency_key)
            if existing is not None and existing.status == "success":
                return existing

            snapshot_bundle = await repository.get_or_create_bundle(
                bundle_id=manifest.bundle_id,
                source=manifest.source.value,
                capture_kind=manifest.capture_kind.value,
                schema_version=manifest.schema_version,
                source_urls=[str(url) for url in manifest.source_urls],
                retrieved_at=manifest.retrieved_at,
                retrieval_outcome=manifest.retrieval_outcome,
                parser_version=manifest.parser_version,
                manifest=manifest.model_dump(mode="json"),
            )

            import_record = await repository.create_import(
                snapshot_bundle_id=snapshot_bundle.id,
                idempotency_key=idempotency_key,
                started_at=self.clock.now(),
                status="running",
            )

            try:
                for notice in notices:
                    await self._import_notice(repository, manifest, snapshot_bundle, notice)
                await repository.mark_import_success(
                    import_record.id, self.clock.now()
                )
                await record_audit_event(
                    session,
                    AuditContext(
                        method="CLI",
                        path="snapshots/import",
                        snapshot_import_id=str(import_record.id),
                    ),
                    AuditEventType.SNAPSHOT_IMPORT_SUCCEEDED,
                    AuditOutcome.SUCCESS,
                    {
                        "bundle_id": manifest.bundle_id,
                        "status": "success",
                        "notice_count": len(notices),
                    },
                )
            except BaseException:
                # No partial application: the UnitOfWork rolls back on exit, so a
                # failed import leaves no database rows and no audit record.
                # The original request is returned unchanged for callers to retry.
                try:
                    METRICS_REGISTRY.counter(
                        "bidscope_snapshot_imports_total",
                        {"source": manifest.source.value, "outcome": "failed"},
                    )
                except Exception:
                    logger.warning("metrics_import_failure_failed", exc_info=True)
                raise

        try:
            METRICS_REGISTRY.counter(
                "bidscope_snapshot_imports_total",
                {"source": manifest.source.value, "outcome": "success"},
            )
        except Exception:
            logger.warning("metrics_import_success_failed", exc_info=True)
        return import_record

    def import_inspect(self, bundle: Path) -> Any:
        """Return the integrity inspection result without writing anything.

        Synchronous: it only wraps :func:`inspect_bundle`, so the CLI can call
        it directly without an event loop.
        """
        return inspect_bundle(bundle.resolve())

    # --------------------------------------------------------- internals

    def _parse(self, bundle: Path) -> list[Any]:
        manifest = _parse.load_manifest(bundle)
        adapter = ADAPTERS.get(manifest.source)
        if adapter is None:
            raise SnapshotImportError(
                f"no adapter registered for source {manifest.source.value!r}"
            )
        return adapter.parse(bundle)

    def _derive_idempotency_key(self, bundle_id: str, notices: list[Any]) -> str:
        """Deterministic idempotency key from bundle identity + notice content.

        Identical bundle content (re-import) produces the same key; any content
        change produces a different key and therefore a distinct import.
        """
        notice_hashes = sorted(_content_hash(notice) for notice in notices)
        material = bundle_id + "|" + ",".join(notice_hashes)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _import_notice(
        self, repository: SnapshotRepository, manifest: Any, snapshot_bundle: Any, notice: Any
    ) -> None:
        content_hash = _content_hash(notice)
        object_key = _payload_object_key(snapshot_bundle.bundle_id, content_hash)
        notice_bytes = _canonical_bytes(notice)

        # Step 2/3: deterministic, content-addressed payload store.
        self.object_store.put_bytes(object_key, notice_bytes)

        # Step 4/5: reuse or create the source notice by (source, external_id).
        source_notice = await repository.get_or_create_source_notice(
            source=manifest.source.value,
            external_id=notice.external_id,
            source_url=str(notice.source_url),
            content_hash=content_hash,
            first_seen_at=self.clock.now(),
            latest_seen_at=self.clock.now(),
        )

        # Step 6: append a version only when content actually changed.
        version = await repository.find_version_by_hash(source_notice.id, content_hash)
        if version is not None:
            return

        version = await repository.create_version(
            source_notice_id=source_notice.id,
            payload_object_key=object_key,
            capture_kind=manifest.capture_kind.value,
            parser_version=notice.parser_version,
            content_hash=content_hash,
            title=notice.title,
            purchaser=notice.purchaser,
            region=notice.region,
            publish_date=notice.publish_time,
            deadline=notice.deadline,
            budget_minor_units=notice.budget.minor_units if notice.budget else None,
            budget_currency=notice.budget.currency if notice.budget else None,
            summary=notice.summary,
            raw_fields=dict(notice.raw_fields),
        )

        # Step 7: evidence rows linked to the version that owns them.
        for evidence in self._build_evidence(notice):
            await repository.create_evidence(
                notice_version_id=version.id,
                text=evidence.text,
                start=evidence.start,
                end=evidence.end,
                span_hash=evidence.span_hash,
            )

    def _build_evidence(self, notice: Any) -> list[Any]:
        """Build per-field evidence spans for a notice.

        Each populated material field becomes an evidence record that points at
        the parsed value. Date/time fields are represented by their canonical
        ISO-8601 form (the P0 contract); later stages may attach precise
        character offsets against the raw source. Spans are anchored to the
        value itself (start=0, end=len) with a content hash.

        ``raw_fields`` are included only when they carry genuine business
        evidence; known metadata keys (e.g. ``synthetic_channel``) are excluded
        so the evidence table is not polluted with provenance tags.
        """
        spans: list[Any] = []
        fields = {
            "title": notice.title,
            "purchaser": notice.purchaser,
            "region": notice.region,
            "summary": notice.summary,
            "publish_time": _format_datetime(notice.publish_time),
            "deadline": _format_datetime(notice.deadline),
            "budget": notice.budget.raw_text if notice.budget else None,
        }
        for text in fields.values():
            if text:
                spans.append(_EvidenceSpan(text=text, start=0, end=len(text)))
        for key, value in notice.raw_fields.items():
            if key in _METADATA_FIELD_KEYS:
                continue
            if isinstance(value, str) and value:
                spans.append(_EvidenceSpan(text=value, start=0, end=len(value)))
        return spans


#: Provenance/metadata keys that must never be recorded as evidence spans.
_METADATA_FIELD_KEYS = frozenset({"synthetic_channel"})


def _format_datetime(value: Any) -> str | None:
    """Return the ISO-8601 form of a datetime, or ``None``."""
    return value.isoformat() if value else None


class _EvidenceSpan:
    """Value object describing a single evidence span."""

    def __init__(self, text: str, start: int, end: int) -> None:
        self.text = text
        self.start = start
        self.end = end
        self.span_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
