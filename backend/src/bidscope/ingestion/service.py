"""One bounded, transactional authorized source acquisition run."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal

from bidscope.audit import AuditEventType, AuditOutcome
from bidscope.clock import Clock, SystemClock
from bidscope.domain.snapshots import AuthorizedSourceContract
from bidscope.ingestion.ccgp import (
    SourceClientError,
    SourcePayloadError,
    SourceRateLimitedError,
)
from bidscope.ingestion.materializer import BundleQuarantineError
from bidscope.ingestion.ports import (
    AcquisitionRepository,
    AuditRecorder,
    AuthorizedSourcePage,
    BundleMaterializer,
    Committer,
    SnapshotImporterPort,
    SourceClient,
    SourceObjectStore,
)
from bidscope.observability import METRICS_REGISTRY
from bidscope.snapshots.importer import SnapshotImportError

MAX_PAGES_PER_RUN = 100
MAX_TRANSIENT_RETRIES = 2
MAX_TRANSIENT_BACKOFF_SECONDS = 30


@dataclass(frozen=True)
class AcquisitionResult:
    run_id: str
    source: str
    status: Literal["success", "failed", "quarantined", "rate_limited"]
    cursor_before: str
    cursor_after: str | None
    request_count: int
    record_count: int
    imported_notice_count: int
    failure_code: str | None = None
    retry_after_seconds: int | None = None


class IngestionService:
    """Coordinate source I/O, immutable materialization, import, and cursor commit."""

    def __init__(
        self,
        *,
        source_client: SourceClient,
        acquisition_repository: AcquisitionRepository,
        object_store: SourceObjectStore,
        materializer: BundleMaterializer,
        importer: SnapshotImporterPort,
        data_contract: AuthorizedSourceContract,
        batch_id: str,
        commit: Committer,
        audit: AuditRecorder,
        clock: Clock | None = None,
        max_pages_per_run: int = 100,
        min_interval_seconds: float = 1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        source: str = "ccgp",
    ) -> None:
        if source != "ccgp":
            raise ValueError("authorized ingestion only supports source='ccgp'")
        if (
            max_pages_per_run <= 0
            or max_pages_per_run > MAX_PAGES_PER_RUN
            or min_interval_seconds < 0
        ):
            raise ValueError("ingestion service bounds are invalid")
        if not batch_id.strip():
            raise ValueError("batch_id must be non-blank")
        self.source_client = source_client
        self.acquisition_repository = acquisition_repository
        self.object_store = object_store
        self.materializer = materializer
        self.importer = importer
        self.data_contract = data_contract
        self.batch_id = batch_id
        self.commit = commit
        self.audit = audit
        self.clock = clock or SystemClock()
        self.max_pages_per_run = max_pages_per_run
        self.min_interval_seconds = min_interval_seconds
        self.sleep = sleep
        self.source = source

    async def run_once(self) -> AcquisitionResult:
        started_at = self.clock.now()
        cursor = await self.acquisition_repository.get_or_create_source_sync_cursor(
            source=self.source, cursor_value="", watermark_at=started_at
        )
        cursor_before = str(cursor.cursor_value)
        run = await self.acquisition_repository.create_acquisition_run(
            source=self.source, started_at=started_at, cursor_before=cursor_before
        )
        run_id = str(run.id)
        started_monotonic = time.monotonic()
        request_count = 0
        record_count = 0
        imported_notice_count = 0
        new_bundle_count = 0
        request_cursor: str | None = cursor_before or None
        last_cursor = cursor_before
        last_retrieved_at = started_at
        response_object_key: str | None = None
        response_sha256: str | None = None
        response_object_keys: list[str] = []
        response_sha256s: list[str] = []
        http_status: int | None = None

        try:
            for page_index in range(self.max_pages_per_run):
                page = await self._fetch_page_with_retry(request_cursor)
                request_count += 1
                record_count += len(page.items)
                last_retrieved_at = page.retrieved_at
                http_status = page.status_code
                actual_response_sha256 = sha256(page.response_bytes).hexdigest()
                page_object_key = f"acquisitions/{self.source}/{actual_response_sha256}.json"
                self.object_store.put_bytes(page_object_key, page.response_bytes)
                response_object_key = page_object_key
                response_sha256 = actual_response_sha256
                response_object_keys.append(page_object_key)
                response_sha256s.append(actual_response_sha256)
                bundle = self.materializer.materialize(
                    page, batch_id=self.batch_id, data_contract=self.data_contract
                )
                import_record = await self.importer.import_bundle(bundle.path)
                metrics = getattr(import_record, "metrics", {})
                imported_notice_count += (
                    int(metrics.get("notice_count", len(page.items)))
                    if isinstance(metrics, Mapping)
                    else len(page.items)
                )
                if getattr(import_record, "_reprocessing", "new") != "reused":
                    new_bundle_count += 1

                if page.next_cursor is None:
                    break
                if page.next_cursor == request_cursor:
                    raise SourcePayloadError("source cursor did not advance")
                last_cursor = page.next_cursor
                request_cursor = page.next_cursor
                if page_index + 1 >= self.max_pages_per_run:
                    raise SourcePayloadError("maximum pages per acquisition run exceeded")
                await self.sleep(self.min_interval_seconds)
            else:
                raise SourcePayloadError("maximum pages per acquisition run exceeded")

            advanced = await self.acquisition_repository.advance_source_sync_cursor(
                source=self.source,
                expected_version=int(cursor.version),
                cursor_before=cursor_before,
                cursor_after=last_cursor,
                watermark_at=last_retrieved_at,
                succeeded_at=self.clock.now(),
            )
            if not advanced:
                raise RuntimeError("source cursor ownership was lost")
        except SourceRateLimitedError as error:
            return await self._finish(
                run_id=run_id,
                status="rate_limited",
                cursor_before=cursor_before,
                cursor_after=None,
                request_count=request_count,
                record_count=record_count,
                imported_notice_count=imported_notice_count,
                new_bundle_count=new_bundle_count,
                response_object_key=response_object_key,
                response_sha256=response_sha256,
                response_object_keys=response_object_keys,
                response_sha256s=response_sha256s,
                http_status=error.status_code or http_status,
                failure_code=error.code,
                retry_after_seconds=error.retry_after_seconds,
                last_retrieved_at=last_retrieved_at,
                started_monotonic=started_monotonic,
            )
        except (BundleQuarantineError, SnapshotImportError, SourcePayloadError) as error:
            return await self._finish(
                run_id=run_id,
                status="quarantined",
                cursor_before=cursor_before,
                cursor_after=None,
                request_count=request_count,
                record_count=record_count,
                imported_notice_count=imported_notice_count,
                new_bundle_count=new_bundle_count,
                response_object_key=response_object_key,
                response_sha256=response_sha256,
                response_object_keys=response_object_keys,
                response_sha256s=response_sha256s,
                http_status=http_status,
                failure_code=getattr(error, "code", "import_failed"),
                last_retrieved_at=last_retrieved_at,
                started_monotonic=started_monotonic,
            )
        except SourceClientError as error:
            return await self._finish(
                run_id=run_id,
                status="failed",
                cursor_before=cursor_before,
                cursor_after=None,
                request_count=request_count,
                record_count=record_count,
                imported_notice_count=imported_notice_count,
                new_bundle_count=new_bundle_count,
                response_object_key=response_object_key,
                response_sha256=response_sha256,
                response_object_keys=response_object_keys,
                response_sha256s=response_sha256s,
                http_status=error.status_code or http_status,
                failure_code=error.code,
                retry_after_seconds=error.retry_after_seconds,
                last_retrieved_at=last_retrieved_at,
                started_monotonic=started_monotonic,
            )
        except Exception:
            return await self._finish(
                run_id=run_id,
                status="failed",
                cursor_before=cursor_before,
                cursor_after=None,
                request_count=request_count,
                record_count=record_count,
                imported_notice_count=imported_notice_count,
                new_bundle_count=new_bundle_count,
                response_object_key=response_object_key,
                response_sha256=response_sha256,
                response_object_keys=response_object_keys,
                response_sha256s=response_sha256s,
                http_status=http_status,
                failure_code="unexpected_error",
                last_retrieved_at=last_retrieved_at,
                started_monotonic=started_monotonic,
            )

        return await self._finish(
            run_id=run_id,
            status="success",
            cursor_before=cursor_before,
            cursor_after=last_cursor if last_cursor != cursor_before else None,
            request_count=request_count,
            record_count=record_count,
            imported_notice_count=imported_notice_count,
            new_bundle_count=new_bundle_count,
            response_object_key=response_object_key,
            response_sha256=response_sha256,
            response_object_keys=response_object_keys,
            response_sha256s=response_sha256s,
            http_status=http_status,
            last_retrieved_at=last_retrieved_at,
            started_monotonic=started_monotonic,
        )

    async def _fetch_page_with_retry(self, cursor: str | None) -> AuthorizedSourcePage:
        """Retry only transient transport failures with bounded exponential backoff."""
        attempts = 0
        while True:
            try:
                return await self.source_client.fetch_page(cursor)
            except SourceClientError as error:
                if (
                    not error.retryable
                    or isinstance(error, SourceRateLimitedError)
                    or attempts >= MAX_TRANSIENT_RETRIES
                ):
                    raise
                delay = min(2**attempts, MAX_TRANSIENT_BACKOFF_SECONDS)
                attempts += 1
                await self.sleep(delay)

    async def _finish(
        self,
        *,
        run_id: str,
        status: Literal["success", "failed", "quarantined", "rate_limited"],
        cursor_before: str,
        cursor_after: str | None,
        request_count: int,
        record_count: int,
        imported_notice_count: int,
        new_bundle_count: int,
        response_object_key: str | None,
        response_sha256: str | None,
        response_object_keys: list[str],
        response_sha256s: list[str],
        http_status: int | None,
        last_retrieved_at: datetime,
        started_monotonic: float,
        failure_code: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> AcquisitionResult:
        finished_at = self.clock.now()
        await self.acquisition_repository.finalize_acquisition_run(
            run_id=run_id,
            finished_at=finished_at,
            status=status,
            cursor_after=cursor_after,
            request_count=request_count,
            record_count=record_count,
            new_bundle_count=new_bundle_count,
            imported_notice_count=imported_notice_count,
            response_object_key=response_object_key,
            response_sha256=response_sha256,
            response_object_keys=response_object_keys,
            response_sha256s=response_sha256s,
            http_status=http_status,
            retry_after_seconds=retry_after_seconds,
            failure_code=failure_code,
        )
        await self.audit(
            {
                "event_type": AuditEventType.SOURCE_ACQUISITION_COMPLETED.value,
                "outcome": (
                    AuditOutcome.SUCCESS.value
                    if status == "success"
                    else AuditOutcome.FAILURE.value
                ),
                "acquisition_id": run_id,
                "source": self.source,
                "status": status,
                "response_sha256": response_sha256,
                "request_count": request_count,
                "record_count": record_count,
                "new_bundle_count": new_bundle_count,
                "imported_notice_count": imported_notice_count,
                "failure_code": failure_code,
            }
        )
        await self.commit()
        duration = min(3600.0, max(0.0, time.monotonic() - started_monotonic))
        self._record_metrics(status, duration, record_count, finished_at, last_retrieved_at)
        return AcquisitionResult(
            run_id=run_id,
            source=self.source,
            status=status,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            request_count=request_count,
            record_count=record_count,
            imported_notice_count=imported_notice_count,
            failure_code=failure_code,
            retry_after_seconds=retry_after_seconds,
        )

    def _record_metrics(
        self,
        status: str,
        duration: float,
        record_count: int,
        finished_at: datetime,
        retrieved_at: datetime,
    ) -> None:
        try:
            labels = {"source": self.source}
            METRICS_REGISTRY.counter(
                "bidscope_acquisition_runs_total", {**labels, "outcome": status}
            )
            METRICS_REGISTRY.observe("bidscope_acquisition_duration_seconds", duration, labels)
            METRICS_REGISTRY.gauge(
                "bidscope_source_freshness_seconds",
                max(0.0, (finished_at - retrieved_at).total_seconds()),
                labels,
            )
            METRICS_REGISTRY.counter(
                "bidscope_acquisition_records_total", labels, amount=record_count
            )
        except Exception:
            return
