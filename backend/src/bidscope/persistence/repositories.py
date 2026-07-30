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
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bidscope.persistence.models import (
    CanonicalNotice,
    NoticeEvidence,
    NoticeVersion,
    SnapshotBundle,
    SnapshotImport,
    SourceAcquisitionRun,
    SourceNotice,
    SourceSyncCursor,
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
        warnings: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> SnapshotImport:
        import_record = SnapshotImport(
            snapshot_bundle_id=snapshot_bundle_id,
            idempotency_key=idempotency_key,
            status=status,
            started_at=started_at,
            warnings=warnings or {},
            metrics=metrics or {},
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


class SourceAcquisitionRepository:
    """Persistence operations for live source cursors and run metadata.

    Methods flush changes but never commit. The caller owns the transaction so
    cursor advancement can happen in the same transaction as the work it
    represents.
    """

    _MAX_STATUS_READ_LIMIT = 100

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_source_sync_cursor(
        self,
        source: str,
        cursor_value: str,
        watermark_at: datetime,
    ) -> SourceSyncCursor:
        """Return the existing cursor or insert the first cursor atomically."""
        existing = await self.get_source_sync_cursor(source)
        if existing is not None:
            return existing

        cursor = SourceSyncCursor(
            source=source,
            cursor_value=cursor_value,
            watermark_at=watermark_at,
        )
        savepoint = await self.session.begin_nested()
        try:
            self.session.add(cursor)
            await self.session.flush()
            await savepoint.commit()
            return cursor
        except IntegrityError:
            await savepoint.rollback()
            existing = await self.get_source_sync_cursor(source)
            if existing is not None:
                return existing
            raise

    async def get_source_sync_cursor(self, source: str) -> SourceSyncCursor | None:
        return await self.session.get(SourceSyncCursor, source)

    async def get_source_sync_cursor_for_update(
        self, source: str
    ) -> SourceSyncCursor | None:
        statement = (
            sa.select(SourceSyncCursor)
            .where(SourceSyncCursor.source == source)
            .with_for_update()
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def advance_source_sync_cursor(
        self,
        source: str,
        *,
        expected_version: int,
        cursor_before: str,
        cursor_after: str,
        watermark_at: datetime,
        succeeded_at: datetime,
    ) -> bool:
        """Advance a cursor only if the caller still owns its prior version."""
        statement = (
            sa.update(SourceSyncCursor)
            .where(
                SourceSyncCursor.source == source,
                SourceSyncCursor.version == expected_version,
                SourceSyncCursor.cursor_value == cursor_before,
            )
            .values(
                cursor_value=cursor_after,
                watermark_at=watermark_at,
                last_success_at=succeeded_at,
                updated_at=succeeded_at,
                consecutive_failures=0,
                version=SourceSyncCursor.version + 1,
            )
        )
        result = await self.session.execute(statement)
        return cast(CursorResult[Any], result).rowcount == 1

    async def create_acquisition_run(
        self,
        source: str,
        started_at: datetime,
        cursor_before: str,
        *,
        status: str = "running",
    ) -> SourceAcquisitionRun:
        run = SourceAcquisitionRun(
            source=source,
            started_at=started_at,
            status=status,
            cursor_before=cursor_before,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def finalize_acquisition_run(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        status: str,
        cursor_after: str | None = None,
        request_count: int | None = None,
        record_count: int | None = None,
        new_bundle_count: int | None = None,
        imported_notice_count: int | None = None,
        response_object_key: str | None = None,
        response_sha256: str | None = None,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        failure_code: str | None = None,
    ) -> SourceAcquisitionRun | None:
        statement = (
            sa.select(SourceAcquisitionRun)
            .where(
                SourceAcquisitionRun.id == run_id,
                SourceAcquisitionRun.status == "running",
            )
            .with_for_update()
        )
        result = await self.session.execute(statement)
        run = result.scalar_one_or_none()
        if run is None:
            return None

        run.finished_at = finished_at
        run.status = status
        run.cursor_after = cursor_after
        if request_count is not None:
            run.request_count = request_count
        if record_count is not None:
            run.record_count = record_count
        if new_bundle_count is not None:
            run.new_bundle_count = new_bundle_count
        if imported_notice_count is not None:
            run.imported_notice_count = imported_notice_count
        if response_object_key is not None:
            run.response_object_key = response_object_key
        if response_sha256 is not None:
            run.response_sha256 = response_sha256
        if http_status is not None:
            run.http_status = http_status
        if retry_after_seconds is not None:
            run.retry_after_seconds = retry_after_seconds
        run.failure_code = failure_code

        if status in {"failed", "quarantined", "rate_limited"}:
            cursor = await self.get_source_sync_cursor_for_update(run.source)
            if cursor is not None:
                cursor.consecutive_failures += 1
                cursor.updated_at = finished_at
                cursor.version += 1

        await self.session.flush()
        return run

    async def list_source_statuses(self, limit: int = 100) -> list[SourceSyncCursor]:
        bounded_limit = self._bounded_limit(limit)
        result = await self.session.execute(
            sa.select(SourceSyncCursor)
            .order_by(SourceSyncCursor.source)
            .limit(bounded_limit)
        )
        return list(result.scalars().all())

    async def get_latest_acquisition_run(
        self, source: str
    ) -> SourceAcquisitionRun | None:
        result = await self.session.execute(
            sa.select(SourceAcquisitionRun)
            .where(SourceAcquisitionRun.source == source)
            .order_by(SourceAcquisitionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_acquisition_runs(
        self, source: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[SourceAcquisitionRun]:
        bounded_limit = self._bounded_limit(limit)
        if offset < 0 or offset > 100_000:
            raise ValueError("offset must be between 0 and 100000")
        statement = sa.select(SourceAcquisitionRun)
        if source is not None:
            statement = statement.where(SourceAcquisitionRun.source == source)
        result = await self.session.execute(
            statement.order_by(
                SourceAcquisitionRun.started_at.desc(), SourceAcquisitionRun.id.desc()
            )
            .offset(offset)
            .limit(bounded_limit)
        )
        return list(result.scalars().all())

    @classmethod
    def _bounded_limit(cls, limit: int) -> int:
        if limit < 1 or limit > cls._MAX_STATUS_READ_LIMIT:
            raise ValueError(f"limit must be between 1 and {cls._MAX_STATUS_READ_LIMIT}")
        return limit
