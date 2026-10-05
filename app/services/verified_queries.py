"""Verified queries: questions the user marked as answered correctly (👍), with their SQL.

Similar later questions get them as worked examples. Matching uses pg_trgm text similarity in
the app database, so it costs no embedding call (free-tier friendly) and catches rephrasings
that share words ("revenue by month" ~ "monthly revenue").
"""
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.exceptions import InvalidInput, NotFound
from app.models.models import QueryLog, VerifiedQuery
from app.services.prompt_context import Example
from app.services.reply_format import parse_reply

MAX_PER_CONNECTION = 300
MIN_SIMILARITY = 0.3
# A verified answer this close to the question is shown as "based on a verified answer".
STRONG_MATCH = 0.75


async def verify(session: AsyncSession, user_id: uuid.UUID, log_id: uuid.UUID) -> VerifiedQuery:
    """Save a successful question of this user's as verified (updates the SQL if re-verified)."""
    log = await session.scalar(
        select(QueryLog).where(QueryLog.id == log_id, QueryLog.user_id == user_id, QueryLog.status == "success")
    )
    sql = parse_reply(log.generated_sql or "").sql if log else ""
    if log is None or not sql:
        raise NotFound(errors.VERIFY_NOT_POSSIBLE)
    count = await session.scalar(
        select(func.count(VerifiedQuery.id)).where(VerifiedQuery.connection_id == log.connection_id)
    ) or 0
    if count >= MAX_PER_CONNECTION:
        raise InvalidInput(errors.VERIFIED_FULL)
    statement = (
        insert(VerifiedQuery)
        .values(id=uuid.uuid4(), connection_id=log.connection_id, user_id=user_id, question=log.nl_query, sql=sql)
        .on_conflict_do_update(constraint="uq_verified_queries_connection_question", set_={"sql": sql})
        .returning(VerifiedQuery)
    )
    verified = (await session.execute(statement)).scalar_one()
    await session.commit()
    return verified


async def list_for_connection(
    session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID
) -> list[VerifiedQuery]:
    result = await session.scalars(
        select(VerifiedQuery)
        .where(VerifiedQuery.connection_id == connection_id, VerifiedQuery.user_id == user_id)
        .order_by(VerifiedQuery.created_at.desc())
    )
    return list(result)


async def verified_log_ids(session: AsyncSession, user_id: uuid.UUID, logs: Sequence[QueryLog]) -> set[uuid.UUID]:
    """The answered `logs` whose question and SQL are saved as verified (one query for a page).

    A re-verified question stores its newer SQL, so an older answer to the same question isn't
    shown as verified."""
    answered = [log for log in logs if log.status == "success"]
    if not answered:
        return set()
    rows = await session.execute(
        select(VerifiedQuery.connection_id, VerifiedQuery.question, VerifiedQuery.sql).where(
            VerifiedQuery.user_id == user_id,
            VerifiedQuery.connection_id.in_({log.connection_id for log in answered}),
            VerifiedQuery.question.in_({log.nl_query for log in answered}),
        )
    )
    saved = {(r.connection_id, r.question): r.sql for r in rows}
    return {
        log.id
        for log in answered
        if (key := (log.connection_id, log.nl_query)) in saved
        and saved[key] == parse_reply(log.generated_sql or "").sql
    }


async def remove(session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, verified_id: uuid.UUID) -> None:
    result = await session.execute(
        delete(VerifiedQuery).where(
            VerifiedQuery.id == verified_id,
            VerifiedQuery.connection_id == connection_id,
            VerifiedQuery.user_id == user_id,
        )
    )
    if result.rowcount == 0:
        raise NotFound(errors.KNOWLEDGE_NOT_FOUND)
    await session.commit()


async def similar(
    session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, question: str, limit: int
) -> list[Example]:
    """The verified questions most like `question`, best first."""
    score = func.similarity(VerifiedQuery.question, question)
    rows = await session.execute(
        select(VerifiedQuery.question, VerifiedQuery.sql, score.label("score"))
        .where(
            VerifiedQuery.connection_id == connection_id,
            VerifiedQuery.user_id == user_id,
            score >= MIN_SIMILARITY,
        )
        .order_by(score.desc())
        .limit(limit)
    )
    return [Example(r.question, r.sql, float(r.score)) for r in rows]
