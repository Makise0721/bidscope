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
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class RunEvent(Base, TimestampMixin):
    __tablename__ = "run_events"

    id: Mapped[str] = _pk()
    query_run_id: Mapped[str] = _fk("query_runs")
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    node: Mapped[str] = mapped_column(sa.Text, nullable=False)
    event: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message: Mapped[str | None] = mapped_column(sa.Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)

    __table_args__ = (
        sa.UniqueConstraint("query_run_id", "seq", name="uq_run_events_query_run_id_seq"),
    )


# ------------------------------------------------------------------ reports ---


class Report(Base, TimestampMixin):
    __tablename__ = "reports"

    id: Mapped[str] = _pk()
    run_id: Mapped[str | None] = mapped_column(sa.Uuid, sa.ForeignKey("query_runs.id"), index=True)
    export_key: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    freshness_window: Mapped[str | None] = mapped_column(sa.Text)
    completeness_warning: Mapped[str | None] = mapped_column(sa.Text)
    generated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    docx_object_key: Mapped[str | None] = mapped_column(sa.Text)


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
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)


class ReportClaimCitation(Base, TimestampMixin):
    __tablename__ = "report_claim_citations"

    id: Mapped[str] = _pk()
    report_claim_id: Mapped[str] = _fk("report_claims")
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
