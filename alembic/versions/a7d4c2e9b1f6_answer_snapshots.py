"""answer snapshots on query logs (History shows what the user saw)

Additive only: one nullable JSONB column. Existing rows keep NULL (no saved answer) and the
currently deployed code is unaffected.

Revision ID: a7d4c2e9b1f6
Revises: 9e3a6c1d4f5b
Create Date: 2026-10-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7d4c2e9b1f6"
down_revision: Union[str, Sequence[str], None] = "9e3a6c1d4f5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("query_logs", sa.Column("answer_snapshot", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("query_logs", "answer_snapshot")
