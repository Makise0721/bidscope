"""add persisted query run execution tokens

Revision ID: e7f8a9b0c1d2
Revises: d8f4a9c2e6b1
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d8f4a9c2e6b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("query_runs", sa.Column("execution_token", sa.Text(), nullable=True))
    op.create_index(
        "ix_query_runs_execution_token",
        "query_runs",
        ["execution_token"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_query_runs_execution_token", table_name="query_runs")
    op.drop_column("query_runs", "execution_token")
