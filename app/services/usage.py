"""Monthly usage counters for plan limits (see .claude/rules/business-logic.md §2).

The window is the calendar month in UTC. Questions are metered by QueryLog rows and
designs by DesignLog rows; connections are counted as currently owned rows.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import ScalarSelect, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import DBConnection, DesignLog, QueryLog


def month_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def questions_this_month(user_id: uuid.UUID) -> ScalarSelect[int]:
    """The user's question count this month, as a subquery (query_meter combines it with
    other checks in one round trip)."""
    return (
        select(func.count(QueryLog.id))
        .where(QueryLog.user_id == user_id, QueryLog.created_at >= month_start_utc())
        .scalar_subquery()
    )


async def count_questions_this_month(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await session.scalar(select(questions_this_month(user_id))) or 0


async def count_designs_this_month(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count(DesignLog.id)).where(
            DesignLog.user_id == user_id, DesignLog.created_at >= month_start_utc()
        )
    ) or 0


async def count_connections(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count(DBConnection.id)).where(DBConnection.user_id == user_id)
    ) or 0
