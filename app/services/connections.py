"""Connection records: ownership-scoped lookups, creation and deletion."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.exceptions import InvalidInput, NotFound
from app.core.security import encrypt
from app.models.models import DBConnection
from app.services.target_db import check_reachable, normalize_url


async def get_for_user(session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID) -> DBConnection:
    """The user's connection, or NotFound (also for other users' ids, so ids can't be probed)."""
    connection = await session.scalar(
        select(DBConnection).where(DBConnection.id == connection_id, DBConnection.user_id == user_id)
    )
    if connection is None:
        raise NotFound(errors.CONNECTION_NOT_FOUND)
    return connection


async def list_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[DBConnection]:
    result = await session.scalars(
        select(DBConnection).where(DBConnection.user_id == user_id).order_by(DBConnection.created_at)
    )
    return list(result)


async def create(session: AsyncSession, user_id: uuid.UUID, name: str, raw_url: str) -> DBConnection:
    """Save a connection only after a successful live connection test."""
    url = normalize_url(raw_url)
    problem = await check_reachable(url)
    if problem is not None:
        raise InvalidInput(problem)
    connection = DBConnection(user_id=user_id, name=name, encrypted_conn_string=encrypt(url))
    session.add(connection)
    await session.commit()
    await session.refresh(connection)
    return connection


async def delete(session: AsyncSession, connection: DBConnection) -> None:
    """Delete a connection; its schema vectors and query logs go with it (ON DELETE CASCADE)."""
    await session.delete(connection)
    await session.commit()
