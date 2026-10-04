"""business context, knowledge items, verified queries, follow-up links (Phase 3)

Additive only: three new tables and one nullable column on query_logs. Existing rows and
the currently deployed code are unaffected.

Revision ID: 9e3a6c1d4f5b
Revises: 8d2f5b0c3e4a
Create Date: 2026-10-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9e3a6c1d4f5b"
down_revision: Union[str, Sequence[str], None] = "8d2f5b0c3e4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _owner_columns() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("db_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "business_contexts",
        *_owner_columns(),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("starter_questions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("setup_calls_day", sa.Date(), nullable=True),
        sa.Column("setup_calls_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("connection_id", name="uq_business_contexts_connection_id"),
    )
    op.create_table(
        "knowledge_items",
        *_owner_columns(),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_items_connection_id", "knowledge_items", ["connection_id"])
    op.create_table(
        "verified_queries",
        *_owner_columns(),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("connection_id", "question", name="uq_verified_queries_connection_question"),
    )
    op.create_index("ix_verified_queries_connection_id", "verified_queries", ["connection_id"])
    op.add_column(
        "query_logs",
        sa.Column(
            "follow_up_of",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("query_logs.id", ondelete="SET NULL", name="fk_query_logs_follow_up_of"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_query_logs_follow_up_of", "query_logs", type_="foreignkey")
    op.drop_column("query_logs", "follow_up_of")
    op.drop_index("ix_verified_queries_connection_id", table_name="verified_queries")
    op.drop_table("verified_queries")
    op.drop_index("ix_knowledge_items_connection_id", table_name="knowledge_items")
    op.drop_table("knowledge_items")
    op.drop_table("business_contexts")
