"""Connections endpoint — CRUD for user DB connections + schema indexing trigger."""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_current_user_with_entitlements, Entitlements
from app.core import errors
from app.core.security import encrypt, decrypt
from app.db.session import get_db
from app.models.models import DBConnection, User
from app.schemas.schemas import DBConnectionCreate, DBConnectionResponse
from app.services.schema_indexer import index_schema

_CONNECT_TIMEOUT_SECONDS = 10

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_asyncpg(conn_str: str) -> str:
    """Rewrite a plain postgresql(s):// or postgres:// URL to use the asyncpg driver
    and strip / translate psycopg2-style query params that asyncpg doesn't understand.

    Uses regex-based param stripping (not urlparse) because non-standard schemes
    like postgresql+asyncpg:// are not reliably parsed by Python's urlparse.

    Handled:
    - Scheme rewritten to postgresql+asyncpg://
    - sslmode removed entirely (asyncpg handles SSL via connect_args, not URL params)
    - All other psycopg2-only / libpq-only params are stripped
    """
    import re as _re

    # 1. Normalise scheme
    for old, new in [
        ("postgresql+psycopg2://", "postgresql+asyncpg://"),
        ("postgresql+psycopg://",  "postgresql+asyncpg://"),
        ("postgres://",            "postgresql+asyncpg://"),
        ("postgresql://",          "postgresql+asyncpg://"),
    ]:
        if conn_str.startswith(old):
            conn_str = new + conn_str[len(old):]
            break

    # 2. Params to strip entirely from the URL query string.
    #    These are psycopg2/libpq params that asyncpg will reject.
    _STRIP_PARAMS = {
        "sslmode", "channel_binding", "options", "application_name",
        "target_session_attrs", "connect_timeout", "fallback_application_name",
        "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count",
        "tcp_user_timeout", "gssencmode", "krbsrvname", "passfile",
    }

    # Strip each forbidden param and any trailing & or leading & left behind
    for param in _STRIP_PARAMS:
        # Matches: param=value at start/?/mid/end of query string
        conn_str = _re.sub(
            rf"([?&]){_re.escape(param)}=[^&]*(&?)",
            lambda m: (m.group(1) if m.group(2) else ""),
            conn_str,
        )

    # Clean up any trailing ? or & with nothing after it
    conn_str = _re.sub(r"[?&]$", "", conn_str)

    return conn_str



@router.get("/", response_model=list[DBConnectionResponse])
async def list_connections(
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
) -> list[DBConnectionResponse]:
    current_user, _ = user_and_ent
    result = await db.execute(
        select(DBConnection).where(DBConnection.user_id == current_user.id)
    )
    connections = result.scalars().all()
    return [DBConnectionResponse.model_validate(c) for c in connections]


class _TestRequest(BaseModel):
    conn_string: str


async def _check_reachable(async_conn_str: str) -> str | None:
    """Open and close one connection. Returns None on success, else a user-safe message."""
    try:
        engine = create_async_engine(
            async_conn_str,
            poolclass=NullPool,
            connect_args={"timeout": _CONNECT_TIMEOUT_SECONDS},
        )
    except Exception as exc:  # malformed URL; the message may contain the password
        logger.info("Connection string rejected: %s", errors.exception_summary(exc))
        return errors.describe_connection_error(exc)

    try:
        async with engine.connect():
            pass
        return None
    except Exception as exc:
        logger.info("Connection test failed: %s", errors.exception_summary(exc))
        return errors.describe_connection_error(exc)
    finally:
        await engine.dispose()


@router.post("/test")
async def test_connection(
    payload: _TestRequest,
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
) -> dict:
    """Quickly validate that a connection string is reachable (does not persist anything)."""
    error = await _check_reachable(_to_asyncpg(payload.conn_string))
    return {"ok": True} if error is None else {"ok": False, "error": error}


@router.post("/", response_model=DBConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    payload: DBConnectionCreate,
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
) -> DBConnectionResponse:
    """Create a new DB connection. Enforces plan connection limits before saving."""
    current_user, entitlements = user_and_ent

    # Enforce plan connection limit
    existing_count = await db.scalar(
        select(func.count(DBConnection.id)).where(DBConnection.user_id == current_user.id)
    )
    if (existing_count or 0) >= entitlements.max_connections:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CONNECTION_LIMIT_REACHED",
        )

    # Normalise the scheme so the async engine always uses asyncpg.
    async_conn_str = _to_asyncpg(payload.connection_string)

    # Test the connection before saving
    error = await _check_reachable(async_conn_str)
    if error is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    namespace = f"user-{current_user.id}-conn-{uuid.uuid4().hex[:8]}"
    connection = DBConnection(
        user_id=current_user.id,
        name=payload.name,
        encrypted_conn_string=encrypt(async_conn_str),
        pinecone_namespace=namespace,
    )
    db.add(connection)
    await db.commit()
    await db.refresh(connection)
    return DBConnectionResponse.model_validate(connection)



@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID,
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
) -> None:
    current_user, _ = user_and_ent
    result = await db.execute(
        select(DBConnection).where(
            DBConnection.id == connection_id,
            DBConnection.user_id == current_user.id,
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    await db.delete(connection)
    await db.commit()


@router.post("/{connection_id}/index")
async def trigger_indexing(
    connection_id: uuid.UUID,
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Trigger schema indexing for a connection. Streams SSE progress events."""
    current_user, _ = user_and_ent
    result = await db.execute(
        select(DBConnection).where(
            DBConnection.id == connection_id,
            DBConnection.user_id == current_user.id,
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

    async def _stream():
        table_count = 0
        async for event in index_schema(
            connection.encrypted_conn_string, connection.pinecone_namespace
        ):
            yield event
            # Parse done event to update DB
            import json
            try:
                data = json.loads(event.removeprefix("data: ").strip())
                if data.get("type") == "done":
                    table_count = data.get("table_count", 0)
            except Exception:
                pass

        # Update connection metadata after indexing
        connection.table_count = table_count
        connection.indexed_at = datetime.now(timezone.utc)
        await db.commit()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
