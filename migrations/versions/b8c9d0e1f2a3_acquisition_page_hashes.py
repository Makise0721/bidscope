"""retain every raw response hash for paginated acquisition runs

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-30 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_acquisition_runs",
        sa.Column("response_object_keys", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column(
        "source_acquisition_runs",
        sa.Column("response_sha256s", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("source_acquisition_runs", "response_sha256s")
    op.drop_column("source_acquisition_runs", "response_object_keys")
