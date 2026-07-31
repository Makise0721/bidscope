"""claim semantic support verifications

Semantic Citation Contract §4/§5: persist the full verdict record of every
semantic verification so UNSUPPORTED/UNCERTAIN claims can be filtered from the
main intelligence list while remaining fully auditable. Adds a ``support_status``
column to ``report_claims`` (nullable — legacy rows were never verified) and a
``report_claim_verifications`` table holding status, rationale, the evidence ids
used, the conflicting evidence ids and the verifier version.

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-07-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "report_claims",
        sa.Column("support_status", sa.Text(), nullable=True),
    )
    op.create_table(
        "report_claim_verifications",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("report_claim_id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_ids_used", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("conflict_evidence_ids", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("verifier_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["report_claim_id"], ["report_claims.id"], name=op.f("fk_report_claim_verifications_report_claim_id_report_claims")),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], name=op.f("fk_report_claim_verifications_report_id_reports")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_claim_verifications")),
    )
    op.create_index(op.f("ix_report_claim_verifications_report_claim_id"), "report_claim_verifications", ["report_claim_id"], unique=False)
    op.create_index(op.f("ix_report_claim_verifications_report_id"), "report_claim_verifications", ["report_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_report_claim_verifications_report_id"), table_name="report_claim_verifications")
    op.drop_index(op.f("ix_report_claim_verifications_report_claim_id"), table_name="report_claim_verifications")
    op.drop_table("report_claim_verifications")
    op.drop_column("report_claims", "support_status")
