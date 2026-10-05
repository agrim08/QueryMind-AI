"""step timings on query logs, for latency reports (Phase 4.3)

Additive only: one nullable JSONB column. Existing rows keep NULL.

Revision ID: c6f2d9a4e8b1
Revises: b3e8f1a6c5d2
Create Date: 2026-10-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6f2d9a4e8b1"
down_revision: Union[str, Sequence[str], None] = "b3e8f1a6c5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("query_logs", sa.Column("timings", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("query_logs", "timings")
