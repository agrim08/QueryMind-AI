"""Connections endpoints — CRUD for a user's database connections + schema indexing."""
from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, OwnedConnection, get_current_user, require_connection_slot
from app.core.exceptions import InvalidInput
from app.core.sse import sse_response
from app.db.session import DbSession
from app.schemas.schemas import (
    ConnectionTestRequest,
    ConnectionTestResponse,
    DBConnectionCreate,
    DBConnectionResponse,
)
from app.services import connections as connection_service
from app.services.schema_indexer import index_connection
from app.services.target_db import check_reachable, normalize_url

router = APIRouter()


@router.get("/", response_model=list[DBConnectionResponse])
async def list_connections(user: CurrentUser, db: DbSession) -> list[DBConnectionResponse]:
    connections = await connection_service.list_for_user(db, user.id)
    return [DBConnectionResponse.model_validate(c) for c in connections]


@router.post("/test", response_model=ConnectionTestResponse, dependencies=[Depends(get_current_user)])
async def test_connection(payload: ConnectionTestRequest) -> ConnectionTestResponse:
    """Check that a connection string is reachable. Nothing is saved."""
    try:
        problem = await check_reachable(normalize_url(payload.conn_string))
    except InvalidInput as exc:
        problem = exc.message
    return ConnectionTestResponse(ok=problem is None, error=problem)


@router.post(
    "/",
    response_model=DBConnectionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_connection_slot)],
)
async def create_connection(payload: DBConnectionCreate, user: CurrentUser, db: DbSession) -> DBConnectionResponse:
    """Save a connection after a live test. Limited by the plan's connection count."""
    connection = await connection_service.create(db, user.id, payload.name, payload.connection_string)
    return DBConnectionResponse.model_validate(connection)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(connection: OwnedConnection, db: DbSession) -> None:
    await connection_service.delete(db, connection)


@router.post("/{connection_id}/index")
async def trigger_indexing(connection: OwnedConnection) -> StreamingResponse:
    """Index the connection's schema, streaming progress as SSE events
    (see app.services.schema_indexer for the event shapes).

    If another run is already indexing this connection, the stream carries a single
    `error` event saying so.
    """
    return sse_response(
        index_connection(connection.id, connection.user_id, connection.encrypted_conn_string)
    )
