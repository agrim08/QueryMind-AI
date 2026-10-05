"""prompt fingerprint on query logs, for reusing earlier answers (Phase 4.1)

Additive only: one nullable column and one index. Existing rows keep NULL (never reused) and
the currently deployed code is unaffected.

Revision ID: b3e8f1a6c5d2
Revises: a7d4c2e9b1f6
Create Date: 2026-10-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3e8f1a6c5d2"
down_revision: Union[str, Sequence[str], None] = "a7d4c2e9b1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("query_logs", sa.Column("prompt_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_query_logs_connection_id_prompt_hash", "query_logs", ["connection_id", "prompt_hash"])


def downgrade() -> None:
    op.drop_index("ix_query_logs_connection_id_prompt_hash", table_name="query_logs")
    op.drop_column("query_logs", "prompt_hash")
