"""The question meter: one QueryLog row per question, reserved before any work is done.

`reserve` runs after the ownership and "indexed" checks and before the pipeline spends
anything (AI call, vector search, target database). Holding a lock on the user's row, it
checks the one-question-at-a-time rule and the plan's monthly limit, then inserts the
question's QueryLog row with status "pending". The lock means two parallel requests can't
both see "49 of 50 used". The row is the usage meter (business-logic.md §2), so a question
counts even if the worker crashes before `finish` runs.

`finish` writes the outcome to that row when the stream ends, including when the client
disconnects mid-answer (the question is then recorded as stopped).
"""
import uuid
from datetime import timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.exceptions import Conflict, LimitReached, NotFound
from app.db.session import AsyncSessionLocal
from app.models.models import QueryLog, User
from app.services import usage
from app.services.query_pipeline import PipelineOutcome

# A pending row older than this belongs to a crashed worker and no longer blocks the user.
# Comfortably above the longest answer: generation, one retry and two 10 s executions.
IN_FLIGHT_WINDOW = timedelta(minutes=2)
# How long a clarifying question can be answered and still continue the same question.
CLARIFICATION_WINDOW = timedelta(minutes=30)


async def _has_question_in_flight(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    QueryLog.user_id == user_id,
                    QueryLog.status == "pending",
                    QueryLog.created_at > func.now() - IN_FLIGHT_WINDOW,
                )
            )
        )
    )


async def reserve(
    session: AsyncSession,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    question: str,
    monthly_limit: int,
) -> uuid.UUID:
    """Reserve the question's QueryLog row, or raise Conflict (one already running) or
    LimitReached("QUERY_LIMIT_REACHED"). Commits the session."""
    # Serialises this user's reservations; other users are unaffected.
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())

    if await _has_question_in_flight(session, user_id):
        await session.rollback()
        raise Conflict(errors.QUESTION_IN_FLIGHT)
    if await usage.count_questions_this_month(session, user_id) >= monthly_limit:
        await session.rollback()
        raise LimitReached("QUERY_LIMIT_REACHED")

    log = QueryLog(user_id=user_id, connection_id=connection_id, nl_query=question, status="pending")
    session.add(log)
    await session.commit()
    return log.id


async def resume(
    session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, log_id: uuid.UUID
) -> QueryLog:
    """Reopen a question that ended with a clarifying question, so the user's answer continues
    it as the same question (no new count). Raises NotFound if it isn't this user's question,
    didn't ask for clarification, or is older than CLARIFICATION_WINDOW; Conflict if another
    question is running. Commits the session."""
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())
    if await _has_question_in_flight(session, user_id):
        await session.rollback()
        raise Conflict(errors.QUESTION_IN_FLIGHT)
    log = await session.scalar(
        select(QueryLog)
        .where(
            QueryLog.id == log_id,
            QueryLog.user_id == user_id,
            QueryLog.connection_id == connection_id,
            QueryLog.status == "clarify",
            QueryLog.created_at > func.now() - CLARIFICATION_WINDOW,
        )
        .with_for_update()
    )
    if log is None:
        await session.rollback()
        raise NotFound(errors.CLARIFICATION_EXPIRED)
    # Pending again, timestamped now so the one-question-at-a-time rule sees it as running.
    log.status = "pending"
    log.created_at = func.now()
    await session.commit()
    await session.refresh(log)
    return log


async def finish(log_id: uuid.UUID, outcome: PipelineOutcome) -> None:
    """Record how the question ended (history + meter), in its own session."""
    status, message = outcome.status, outcome.error_message
    if status == "pending":  # the stream ended before an outcome: the client went away
        status, message = "error", errors.QUESTION_STOPPED
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(QueryLog)
            .where(QueryLog.id == log_id)
            .values(
                generated_sql=outcome.generated_sql or None,
                row_count=outcome.result.row_count if outcome.result else None,
                exec_time_ms=outcome.result.exec_time_ms if outcome.result else None,
                status=status,
                error_message=message,
            )
        )
        await session.commit()
