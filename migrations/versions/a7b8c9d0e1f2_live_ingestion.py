"""persist live source cursors and acquisition runs

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-07-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_sync_cursors",
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("cursor_value", sa.String(length=512), nullable=False),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("source IN ('ccgp')", name="source_allowed"),
        sa.CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
        sa.CheckConstraint("version >= 0", name="version_nonnegative"),
        sa.PrimaryKeyConstraint("source", name=op.f("pk_source_sync_cursors")),
    )

    op.create_table(
        "source_acquisition_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cursor_before", sa.String(length=512), nullable=False),
        sa.Column("cursor_after", sa.String(length=512), nullable=True),
        sa.Column("request_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("record_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("new_bundle_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "imported_notice_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("response_object_key", sa.String(length=512), nullable=True),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_acquisition_runs")),
    )
    op.create_index(
        "ix_source_acquisition_runs_source",
        "source_acquisition_runs",
        ["source"],
        unique=False,
    )
    op.create_index(
        "ix_source_acquisition_runs_started_at_desc",
        "source_acquisition_runs",
        [sa.text("started_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_acquisition_runs_started_at_desc",
        table_name="source_acquisition_runs",
    )
    op.drop_index("ix_source_acquisition_runs_source", table_name="source_acquisition_runs")
    op.drop_table("source_acquisition_runs")
    op.drop_table("source_sync_cursors")
