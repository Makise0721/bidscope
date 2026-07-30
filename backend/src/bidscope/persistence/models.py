from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_UUID_DEFAULT = sa.text("gen_random_uuid()")
_NOW_DEFAULT = sa.text("now()")
_EMPTY_JSON = sa.text("'{}'::jsonb")
_JSON_ARRAY = sa.text("'[]'::jsonb")


def _pk() -> Any:
    return mapped_column(sa.Uuid, primary_key=True, server_default=_UUID_DEFAULT)


def _fk(table: str, nullable: bool = False) -> Any:
    return mapped_column(sa.Uuid, sa.ForeignKey(f"{table}.id"), index=True, nullable=nullable)


metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(DeclarativeBase):
    metadata = metadata


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_NOW_DEFAULT, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_NOW_DEFAULT, nullable=False
    )


# ---------------------------------------------------------------- notices ---


class CanonicalNotice(Base, TimestampMixin):
    __tablename__ = "canonical_notices"

    id: Mapped[str] = _pk()
    title: Mapped[str | None] = mapped_column(sa.Text)


class SourceNotice(Base, TimestampMixin):
    __tablename__ = "source_notices"

    id: Mapped[str] = _pk()
    canonical_notice_id: Mapped[str] = _fk("canonical_notices")
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    external_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    latest_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    project_number: Mapped[str | None] = mapped_column(sa.Text)
    purchaser: Mapped[str | None] = mapped_column(sa.Text)
    region: Mapped[str | None] = mapped_column(sa.Text)
    title: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.UniqueConstraint("source", "external_id", name="uq_source_notices_source_external_id"),
    )


class NoticeVersion(Base, TimestampMixin):
    __tablename__ = "notice_versions"

    id: Mapped[str] = _pk()
    source_notice_id: Mapped[str] = _fk("source_notices")
    payload_object_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    capture_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title: Mapped[str | None] = mapped_column(sa.Text)
    purchaser: Mapped[str | None] = mapped_column(sa.Text)
    region: Mapped[str | None] = mapped_column(sa.Text)
    publish_date: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    budget_minor_units: Mapped[int | None] = mapped_column(sa.BigInteger)
    budget_currency: Mapped[str | None] = mapped_column(sa.Text)
    summary: Mapped[str | None] = mapped_column(sa.Text)
    raw_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))

    __table_args__ = (
        sa.UniqueConstraint(
            "source_notice_id",
            "content_hash",
            name="uq_notice_versions_source_notice_id_content_hash",
        ),
        # Trigram GIN index for lexical title recall (pg_trgm similarity).
        sa.Index(
            "ix_notice_versions_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        # HNSW index for pgvector cosine recall over notice embeddings.
        sa.Index(
            "ix_notice_versions_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class NoticeEvidence(Base, TimestampMixin):
    __tablename__ = "notice_evidence"

    id: Mapped[str] = _pk()
    notice_version_id: Mapped[str] = _fk("notice_versions")
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    start: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    end: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    span_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)


# --------------------------------------------------------------- snapshots ---


class SnapshotBundle(Base, TimestampMixin):
    __tablename__ = "snapshot_bundles"

    id: Mapped[str] = _pk()
    bundle_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    capture_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    source_urls: Mapped[list[str]] = mapped_column(JSONB, server_default=_JSON_ARRAY)
    retrieved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    retrieval_outcome: Mapped[str] = mapped_column(sa.Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)


class SnapshotImport(Base, TimestampMixin):
    __tablename__ = "snapshot_imports"

    id: Mapped[str] = _pk()
    snapshot_bundle_id: Mapped[str] = _fk("snapshot_bundles")
    idempotency_key: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    warnings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


# --------------------------------------------------------------------- runs ---


class QueryRun(Base, TimestampMixin):
    __tablename__ = "query_runs"

    id: Mapped[str] = _pk()
    run_key: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_request: Mapped[str] = mapped_column(sa.Text, nullable=False)
    search_intent: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    checkpoint_thread_id: Mapped[str | None] = mapped_column(sa.Text)
    execution_token: Mapped[str | None] = mapped_column(sa.Text, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class RunEvent(Base, TimestampMixin):
    __tablename__ = "run_events"

    id: Mapped[str] = _pk()
    query_run_id: Mapped[str] = _fk("query_runs")
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: Clock-injected moment the graph node emitted this event. Kept separate
    #: from ``created_at`` (server insert time) so audit records reflect when
    #: the event actually happened in the run, matching the node_events streamed
    #: to the API.
    timestamp: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False,
    )
    node: Mapped[str] = mapped_column(sa.Text, nullable=False)
    event: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message: Mapped[str | None] = mapped_column(sa.Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)

    __table_args__ = (
        sa.UniqueConstraint("query_run_id", "seq", name="uq_run_events_query_run_id_seq"),
    )


class AuditEvent(Base):
    """Bounded security and lifecycle event metadata without business FKs."""

    __tablename__ = "audit_events"

    id: Mapped[str] = _pk()
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_NOW_DEFAULT, nullable=False
    )
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(sa.Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(sa.Text, index=True)
    method: Mapped[str | None] = mapped_column(sa.Text)
    path: Mapped[str | None] = mapped_column(sa.Text)
    run_id: Mapped[str | None] = mapped_column(sa.Text, index=True)
    subscription_id: Mapped[str | None] = mapped_column(sa.Text, index=True)
    report_id: Mapped[str | None] = mapped_column(sa.Text, index=True)
    snapshot_import_id: Mapped[str | None] = mapped_column(sa.Text, index=True)
    error_code: Mapped[str | None] = mapped_column(sa.Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)

    __table_args__ = (
        sa.Index("ix_audit_events_occurred_at", "occurred_at"),
    )


# ---------------------------------------------------------- live ingestion ---


class SourceSyncCursor(Base):
    """Durable per-source cursor state for live acquisition workers."""

    __tablename__ = "source_sync_cursors"

    # The source is the natural key: a cursor is unique per source and has no
    # separate identity that callers need to handle.
    source: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    cursor_value: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    watermark_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    last_success_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_NOW_DEFAULT, nullable=False
    )
    consecutive_failures: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
    version: Mapped[int] = mapped_column(
        sa.BigInteger, server_default=sa.text("0"), nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint("source IN ('ccgp')", name="source_allowed"),
        sa.CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
        sa.CheckConstraint("version >= 0", name="version_nonnegative"),
    )


class SourceAcquisitionRun(Base):
    """Bounded metadata for one source acquisition attempt.

    This record intentionally contains no response bodies, request headers,
    credentials, query strings, or exception tracebacks.
    """

    __tablename__ = "source_acquisition_runs"

    id: Mapped[str] = _pk()
    source: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    cursor_before: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    cursor_after: Mapped[str | None] = mapped_column(sa.String(512))
    request_count: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
    record_count: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
    new_bundle_count: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
    imported_notice_count: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )
    response_object_key: Mapped[str | None] = mapped_column(sa.String(512))
    response_sha256: Mapped[str | None] = mapped_column(sa.String(64))
    response_object_keys: Mapped[list[str]] = mapped_column(
        JSONB, server_default=_JSON_ARRAY, nullable=False
    )
    response_sha256s: Mapped[list[str]] = mapped_column(
        JSONB, server_default=_JSON_ARRAY, nullable=False
    )
    http_status: Mapped[int | None] = mapped_column(sa.SmallInteger)
    retry_after_seconds: Mapped[int | None] = mapped_column(sa.Integer)
    failure_code: Mapped[str | None] = mapped_column(sa.String(64))

    __table_args__ = (
        sa.CheckConstraint("source IN ('ccgp')", name="source_allowed"),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'failed', 'quarantined', 'rate_limited')",
            name="status_allowed",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR failure_code ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'",
            name="failure_code_format",
        ),
        sa.CheckConstraint(
            "response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$'",
            name="response_sha256_format",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="http_status_range",
        ),
        sa.CheckConstraint(
            "retry_after_seconds IS NULL OR retry_after_seconds >= 0",
            name="retry_after_nonnegative",
        ),
        sa.CheckConstraint("request_count >= 0", name="request_count_nonnegative"),
        sa.CheckConstraint("record_count >= 0", name="record_count_nonnegative"),
        sa.CheckConstraint("new_bundle_count >= 0", name="new_bundle_count_nonnegative"),
        sa.CheckConstraint(
            "imported_notice_count >= 0", name="imported_notice_count_nonnegative"
        ),
        sa.Index("ix_source_acquisition_runs_source", "source"),
        sa.Index(
            "ix_source_acquisition_runs_started_at_desc",
            sa.text("started_at DESC"),
        ),
    )


# ------------------------------------------------------------------ reports ---


class Report(Base, TimestampMixin):
    __tablename__ = "reports"

    id: Mapped[str] = _pk()
    run_id: Mapped[str | None] = mapped_column(sa.Uuid, sa.ForeignKey("query_runs.id"))
    export_key: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    freshness_window: Mapped[str | None] = mapped_column(sa.Text)
    source_availability: Mapped[list[str]] = mapped_column(
        JSONB, server_default=_JSON_ARRAY, nullable=False
    )
    completeness_warning: Mapped[str | None] = mapped_column(sa.Text)
    generated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    docx_object_key: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (sa.UniqueConstraint("run_id", name="uq_reports_run_id"),)


class ReportItem(Base, TimestampMixin):
    __tablename__ = "report_items"

    id: Mapped[str] = _pk()
    report_id: Mapped[str] = _fk("reports")
    rank: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    notice_version_id: Mapped[str] = _fk("notice_versions")
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    known_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    unknown_fields: Mapped[list[str]] = mapped_column(JSONB, server_default=_JSON_ARRAY)
    relevance_reason: Mapped[str | None] = mapped_column(sa.Text)
    risk_note: Mapped[str | None] = mapped_column(sa.Text)


class ReportClaim(Base, TimestampMixin):
    __tablename__ = "report_claims"

    id: Mapped[str] = _pk()
    report_item_id: Mapped[str] = _fk("report_items")
    ordinal: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"), nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)


class ReportClaimCitation(Base, TimestampMixin):
    __tablename__ = "report_claim_citations"

    id: Mapped[str] = _pk()
    report_claim_id: Mapped[str] = _fk("report_claims")
    ordinal: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"), nullable=False)
    evidence_id: Mapped[str] = mapped_column(
        sa.Uuid, sa.ForeignKey("notice_evidence.id"), index=True
    )
    label: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.UniqueConstraint(
            "report_claim_id",
            "evidence_id",
            name="uq_report_claim_citations_report_claim_id_evidence_id",
        ),
    )


class ReportCitation(Base, TimestampMixin):
    __tablename__ = "report_citations"

    id: Mapped[str] = _pk()
    report_item_id: Mapped[str] = _fk("report_items")
    ordinal: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"), nullable=False)
    evidence_id: Mapped[str] = mapped_column(
        sa.Uuid, sa.ForeignKey("notice_evidence.id"), index=True
    )
    label: Mapped[str | None] = mapped_column(sa.Text)
    span_start: Mapped[int | None] = mapped_column(sa.Integer)
    span_end: Mapped[int | None] = mapped_column(sa.Integer)


# ------------------------------------------------------------ subscriptions ---


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"

    id: Mapped[str] = _pk()
    cron_expression: Mapped[str] = mapped_column(sa.Text, nullable=False)
    timezone: Mapped[str] = mapped_column(
        sa.Text, server_default=sa.text("'Asia/Shanghai'"), nullable=False
    )
    normalized_intent: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    last_successful_run_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    trigger_key: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)


class SubscriptionSeenItem(Base, TimestampMixin):
    __tablename__ = "subscription_seen_items"

    id: Mapped[str] = _pk()
    subscription_id: Mapped[str] = _fk("subscriptions")
    notice_id: Mapped[str] = _fk("source_notices")
    version_content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "subscription_id",
            "notice_id",
            name="uq_subscription_seen_items_subscription_id_notice_id",
        ),
    )


class InboxEvent(Base, TimestampMixin):
    __tablename__ = "inbox_events"

    id: Mapped[str] = _pk()
    subscription_id: Mapped[str | None] = _fk("subscriptions", nullable=True)
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    notice_id: Mapped[str | None] = _fk("source_notices", nullable=True)
    title: Mapped[str | None] = mapped_column(sa.Text)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    read: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.text("false"), nullable=False
    )


# -------------------------------------------------------------------- eval ---


class EvalCase(Base, TimestampMixin):
    __tablename__ = "eval_cases"

    id: Mapped[str] = _pk()
    dataset_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    case_key: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    expected_outputs: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default=_EMPTY_JSON)


class EvalRun(Base, TimestampMixin):
    __tablename__ = "eval_runs"

    id: Mapped[str] = _pk()
    dataset_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    environment: Mapped[str | None] = mapped_column(sa.Text)
    pricing_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
