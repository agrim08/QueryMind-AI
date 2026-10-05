"""Reusing earlier answers: the same question, asked with exactly the same prompt, reuses the
model's earlier reply instead of calling Gemini again (Phase 4.1).

The key is the fingerprint of the exact Gemini request (`sql_generator.request_fingerprint`):
model, system prompt, the tables sent, business definitions, verified examples, conversation
and clarification. Re-indexing, editing a definition, verifying an answer or changing the prompt
or model all change it, so nothing stale is ever reused, and no separate invalidation is needed.

Only the reply is reused, never result rows: the SQL is validated and run again, so the data is
always current. A reused answer still counts as a question (the meter counts questions, not model
calls; business-logic.md §2). The user can ask for a new query instead (`fresh`).
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import QueryLog


async def earlier_reply(
    session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, fingerprint: str
) -> str | None:
    """The newest successful reply this user got to exactly this request on this connection."""
    return await session.scalar(
        select(QueryLog.generated_sql)
        .where(
            QueryLog.connection_id == connection_id,
            QueryLog.user_id == user_id,
            QueryLog.prompt_hash == fingerprint,
            QueryLog.status == "success",
            QueryLog.generated_sql.is_not(None),
        )
        .order_by(QueryLog.created_at.desc())
        .limit(1)
    )
