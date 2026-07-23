"""complete report delivery persistence

Revision ID: d8f4a9c2e6b1
Revises: a1b2c3d4e5f6
Create Date: 2026-07-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d8f4a9c2e6b1"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DUPLICATE_RUN_ID_SAMPLE_LIMIT = 10


def _raise_for_duplicate_report_run_ids() -> None:
    """Fail with remediable context before adding the one-report-per-run rule."""
    rows = op.get_bind().execute(
        sa.text(
            "SELECT run_id::text AS run_id, count(*) AS report_count "
            "FROM reports "
            "WHERE run_id IS NOT NULL "
            "GROUP BY run_id "
            "HAVING count(*) > 1 "
            "ORDER BY count(*) DESC, run_id::text "
            "LIMIT :sample_limit"
        ),
        {"sample_limit": _DUPLICATE_RUN_ID_SAMPLE_LIMIT},
    )
    duplicates = [f"{row.run_id} ({row.report_count})" for row in rows]
    if duplicates:
        raise RuntimeError(
            "Cannot add unique reports.run_id constraint: found duplicate non-null "
            "reports.run_id values "
            f"(first {_DUPLICATE_RUN_ID_SAMPLE_LIMIT}): {', '.join(duplicates)}. "
            "Resolve duplicate reports before retrying this migration."
        )


def upgrade() -> None:
    _raise_for_duplicate_report_run_ids()
    op.add_column(
        "reports",
        sa.Column(
            "source_availability",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "report_claims",
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "report_citations",
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "report_claim_citations",
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.drop_index(op.f("ix_reports_run_id"), table_name="reports")
    op.create_unique_constraint("uq_reports_run_id", "reports", ["run_id"])


def downgrade() -> None:
    op.drop_constraint("uq_reports_run_id", "reports", type_="unique")
    op.create_index(op.f("ix_reports_run_id"), "reports", ["run_id"], unique=False)
    op.drop_column("report_claim_citations", "ordinal")
    op.drop_column("report_citations", "ordinal")
    op.drop_column("report_claims", "ordinal")
    op.drop_column("reports", "source_availability")
