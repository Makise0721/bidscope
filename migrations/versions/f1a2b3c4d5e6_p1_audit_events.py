"""add bounded audit event persistence

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("subscription_id", sa.Text(), nullable=True),
        sa.Column("report_id", sa.Text(), nullable=True),
        sa.Column("snapshot_import_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    for name, column in (
        ("ix_audit_events_occurred_at", "occurred_at"),
        ("ix_audit_events_event_type", "event_type"),
        ("ix_audit_events_request_id", "request_id"),
        ("ix_audit_events_run_id", "run_id"),
        ("ix_audit_events_subscription_id", "subscription_id"),
        ("ix_audit_events_report_id", "report_id"),
        ("ix_audit_events_snapshot_import_id", "snapshot_import_id"),
    ):
        op.create_index(name, "audit_events", [column], unique=False)


def downgrade() -> None:
    for name in (
        "ix_audit_events_snapshot_import_id",
        "ix_audit_events_report_id",
        "ix_audit_events_subscription_id",
        "ix_audit_events_run_id",
        "ix_audit_events_request_id",
        "ix_audit_events_event_type",
        "ix_audit_events_occurred_at",
    ):
        op.drop_index(name, table_name="audit_events")
    op.drop_table("audit_events")
