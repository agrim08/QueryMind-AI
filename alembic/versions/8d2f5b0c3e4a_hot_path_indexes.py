"""indexes for per-user lookups on hot paths

Quota checks count a user's logs for the current month on every question and design;
history pages sort by created_at; every endpoint lists a user's connections.
Additive only.

Revision ID: 8d2f5b0c3e4a
Revises: 7c1e4a9b2d3f
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op

revision: str = "8d2f5b0c3e4a"
down_revision: Union[str, Sequence[str], None] = "7c1e4a9b2d3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_query_logs_user_id_created_at", "query_logs", ["user_id", "created_at"])
    op.create_index("ix_design_logs_user_id_created_at", "design_logs", ["user_id", "created_at"])
    op.create_index("ix_db_connections_user_id", "db_connections", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_db_connections_user_id", table_name="db_connections")
    op.drop_index("ix_design_logs_user_id_created_at", table_name="design_logs")
    op.drop_index("ix_query_logs_user_id_created_at", table_name="query_logs")
