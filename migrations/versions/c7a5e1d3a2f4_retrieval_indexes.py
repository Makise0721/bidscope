"""retrieval indexes

Revision ID: c7a5e1d3a2f4
Revises: b44cc923895e
Create Date: 2026-07-19 15:30:00.000000

"""
from typing import Sequence, Union

import pgvector.sqlalchemy.vector  # noqa: F401
from alembic import op

revision: str = "c7a5e1d3a2f4"
down_revision: Union[str, None] = "b44cc923895e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Trigram GIN index for lexical title recall (pg_trgm similarity).
    op.create_index(
        op.f("ix_notice_versions_title_trgm"),
        "notice_versions",
        ["title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    # HNSW index for pgvector cosine recall over notice embeddings.
    op.create_index(
        op.f("ix_notice_versions_embedding_hnsw"),
        "notice_versions",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_notice_versions_embedding_hnsw"), table_name="notice_versions")
    op.drop_index(op.f("ix_notice_versions_title_trgm"), table_name="notice_versions")
