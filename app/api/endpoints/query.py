"""Query endpoints — ask a question (SSE stream) and read query history."""
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import CurrentUser, require_query_quota
from app.core import errors
from app.core.background import spawn
from app.core.exceptions import InvalidInput
from app.core.sse import sse_response
from app.db.session import DbSession
from app.models.models import QueryLog
from app.schemas.schemas import QueryLogResponse, QueryRequest
from app.services import connections as connection_service
from app.services.query_pipeline import PipelineOutcome, record_query, run_pipeline
from app.services.schema_store import has_elements

router = APIRouter()


@router.post("/", dependencies=[Depends(require_query_quota)])
async def run_query(payload: QueryRequest, user: CurrentUser, db: DbSession) -> StreamingResponse:
    """Answer a question about one of the user's connections, streamed as SSE events
    (see app.services.query_pipeline for the event shapes).

    Rejected before streaming when the plan's monthly limit is reached (403), the
    connection isn't the user's (404) or hasn't been indexed yet (400).
    """
    connection = await connection_service.get_for_user(db, user.id, payload.connection_id)
    if not await has_elements(db, connection.id):
        raise InvalidInput(errors.SCHEMA_NOT_INDEXED)

    async def events() -> AsyncIterator[dict]:
        outcome = PipelineOutcome()
        try:
            async for event in run_pipeline(
                db, connection.id, connection.encrypted_conn_string, payload.nl_query, outcome
            ):
                yield event
        finally:
            # The QueryLog row is the usage meter; write it even if the client disconnects.
            spawn(record_query(user.id, connection.id, payload.nl_query, outcome), name="record-query")

    return sse_response(events())


@router.get("/history", response_model=list[QueryLogResponse])
async def get_history(
    user: CurrentUser,
    db: DbSession,
    connection_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[QueryLogResponse]:
    """The user's query history, newest first."""
    query = select(QueryLog).where(QueryLog.user_id == user.id)
    if connection_id:
        query = query.where(QueryLog.connection_id == connection_id)
    logs = await db.scalars(
        query.order_by(QueryLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return [QueryLogResponse.model_validate(log) for log in logs]
