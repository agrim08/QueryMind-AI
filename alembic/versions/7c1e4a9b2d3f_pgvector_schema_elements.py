"""pgvector: schema_elements table replaces Pinecone; indexing claim column

Additive only (expand step). Existing rows are not modified and nothing is dropped.
The legacy db_connections.pinecone_namespace column stays until a later contract
migration, once the pgvector code has been running in production.

Revision ID: 7c1e4a9b2d3f
Revises: 5114ba2a24b9
Create Date: 2026-10-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.types import HalfVector

revision: str = "7c1e4a9b2d3f"
down_revision: Union[str, Sequence[str], None] = "5114ba2a24b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIMENSIONS = 768  # frozen here on purpose: migrations must not change with app config

SEARCH_TEXT = (
    "to_tsvector('simple'::regconfig, coalesce(schema_name, '') || ' ' || table_name"
    " || ' ' || coalesce(column_name, '') || ' ' || doc)"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.add_column(
        "db_connections",
        sa.Column("indexing_started_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "schema_elements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("schema_name", sa.String(length=255), nullable=False),
        sa.Column("table_name", sa.String(length=255), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=True),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("embedding", HalfVector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("search_tsv", postgresql.TSVECTOR(), sa.Computed(SEARCH_TEXT, persisted=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["db_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schema_elements_connection_id", "schema_elements", ["connection_id"])
    op.create_index(
        "ix_schema_elements_search_tsv", "schema_elements", ["search_tsv"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_schema_elements_search_tsv", table_name="schema_elements")
    op.drop_index("ix_schema_elements_connection_id", table_name="schema_elements")
    op.drop_table("schema_elements")
    op.drop_column("db_connections", "indexing_started_at")
    # Extensions are left installed; dropping them could affect other objects.
