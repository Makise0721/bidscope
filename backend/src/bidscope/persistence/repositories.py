"""Persistence repositories for snapshot import.

The :class:`SnapshotRepository` wraps a single SQLAlchemy async session and owns
all reads and writes for the snapshot import path. It is deliberately free of
transaction boundaries — the caller (the importer) brackets work in a
:class:`UnitOfWork` so that a single import either commits entirely or rolls
back entirely.

All idempotency keys and content hashes are supplied by the caller; the
repository never generates random defaults, consistent with the schema's
``NOT NULL, no random default`` idempotency-key columns.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bidscope.persistence.models import (
    CanonicalNotice,
    NoticeEvidence,
    NoticeVersion,
    SnapshotBundle,
    SnapshotImport,
    SourceNotice,
)


class SnapshotRepository:
    """Data-access layer for snapshot bundles, notices, versions and evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------- bundles

    async def find_bundle(self, bundle_id: str) -> SnapshotBundle | None:
        return await self.session.get(SnapshotBundle, bundle_id)

    async def find_bundle_by_external_id(self, bundle_id: str) -> SnapshotBundle | None:
        statement = sa.select(SnapshotBundle).where(SnapshotBundle.bundle_id == bundle_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_or_create_bundle(
        self,
        bundle_id: str,
        source: str,
        capture_kind: str,
        schema_version: int,
        source_urls: list[str],
        retrieved_at: datetime | None,
        retrieval_outcome: str,
        parser_version: str,
        manifest: dict[str, object],
    ) -> SnapshotBundle:
        existing = await self.find_bundle_by_external_id(bundle_id)
        if existing is not None:
            return existing
        bundle = SnapshotBundle(
            bundle_id=bundle_id,
            source=source,
            capture_kind=capture_kind,
            schema_version=schema_version,
            source_urls=source_urls,
            retrieved_at=retrieved_at,
            retrieval_outcome=retrieval_outcome,
            parser_version=parser_version,
            manifest=manifest,
        )
        self.session.add(bundle)
        await self.session.flush()
        return bundle

    # ------------------------------------------------------------ imports

    async def find_import(self, idempotency_key: str) -> SnapshotImport | None:
        statement = sa.select(SnapshotImport).where(
            SnapshotImport.idempotency_key == idempotency_key
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def create_import(
        self,
        snapshot_bundle_id: str,
        idempotency_key: str,
        started_at: datetime,
        status: str = "running",
    ) -> SnapshotImport:
        import_record = SnapshotImport(
            snapshot_bundle_id=snapshot_bundle_id,
            idempotency_key=idempotency_key,
            status=status,
            started_at=started_at,
        )
        self.session.add(import_record)
        await self.session.flush()
        return import_record

    async def mark_import_success(self, import_id: str, finished_at: datetime) -> None:
        import_record = await self.session.get(SnapshotImport, import_id)
        if import_record is not None:
            import_record.status = "success"
            import_record.finished_at = finished_at

    # ---------------------------------------------------- source notices

    async def find_source_notice(
        self, source: str, external_id: str
    ) -> SourceNotice | None:
        statement = sa.select(SourceNotice).where(
            SourceNotice.source == source, SourceNotice.external_id == external_id
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_or_create_source_notice(
        self,
        source: str,
        external_id: str,
        source_url: str,
        content_hash: str,
        first_seen_at: datetime,
        latest_seen_at: datetime,
    ) -> SourceNotice:
        """Find an existing source notice or insert one atomically-ish.

        P0 imports run single-process via the CLI, so the check-then-insert
        race is not exercised today. The insert is nevertheless wrapped in a
        savepoint so that a future concurrent import which loses the race on
        ``uq_source_notices_source_external_id`` can roll the insert back
        without aborting the wider transaction, then reuse the existing row.
        """
        existing = await self.find_source_notice(source, external_id)
        if existing is not None:
            existing.latest_seen_at = latest_seen_at
            existing.content_hash = content_hash
            return existing
        savepoint = await self.session.begin_nested()
        try:
            canonical = CanonicalNotice()
            self.session.add(canonical)
            await self.session.flush()
            notice = SourceNotice(
                canonical_notice_id=canonical.id,
                source=source,
                external_id=external_id,
                source_url=source_url,
                first_seen_at=first_seen_at,
                latest_seen_at=latest_seen_at,
                content_hash=content_hash,
            )
            self.session.add(notice)
            await self.session.flush()
            return notice
        except IntegrityError:
            # Lost the race: another transaction inserted the same
            # (source, external_id) first. Reuse its row instead of failing.
            await savepoint.rollback()
            existing = await self.find_source_notice(source, external_id)
            if existing is not None:
                existing.latest_seen_at = latest_seen_at
                existing.content_hash = content_hash
                return existing
            raise

    # ---------------------------------------------------- notice versions

    async def find_version_by_hash(
        self, source_notice_id: str, content_hash: str
    ) -> NoticeVersion | None:
        statement = sa.select(NoticeVersion).where(
            NoticeVersion.source_notice_id == source_notice_id,
            NoticeVersion.content_hash == content_hash,
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def create_version(
        self,
        source_notice_id: str,
        payload_object_key: str,
        capture_kind: str,
        parser_version: str,
        content_hash: str,
        title: str | None,
        purchaser: str | None,
        region: str | None,
        publish_date: datetime | None,
        deadline: datetime | None,
        budget_minor_units: int | None,
        budget_currency: str | None,
        summary: str | None,
        raw_fields: dict[str, object],
    ) -> NoticeVersion:
        version = NoticeVersion(
            source_notice_id=source_notice_id,
            payload_object_key=payload_object_key,
            capture_kind=capture_kind,
            parser_version=parser_version,
            content_hash=content_hash,
            title=title,
            purchaser=purchaser,
            region=region,
            publish_date=publish_date,
            deadline=deadline,
            budget_minor_units=budget_minor_units,
            budget_currency=budget_currency,
            summary=summary,
            raw_fields=raw_fields,
        )
        self.session.add(version)
        await self.session.flush()
        return version

    # ----------------------------------------------------------- evidence

    async def create_evidence(
        self,
        notice_version_id: str,
        text: str,
        start: int,
        end: int,
        span_hash: str,
    ) -> NoticeEvidence:
        evidence = NoticeEvidence(
            notice_version_id=notice_version_id,
            text=text,
            start=start,
            end=end,
            span_hash=span_hash,
        )
        self.session.add(evidence)
        await self.session.flush()
        return evidence
