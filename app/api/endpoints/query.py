"""Query endpoints — ask a question (SSE stream) and read query history."""
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import CurrentUser, Plan, rate_limited
from app.core import errors
from app.core.background import spawn
from app.core.exceptions import InvalidInput
from app.core.sse import sse_response
from app.db.session import DbSession
from app.models.models import QueryLog
from app.schemas.schemas import QueryLogResponse, QueryRequest
from app.services import connections as connection_service
from app.services import query_meter
from app.services.query_pipeline import PipelineOutcome, run_pipeline
from app.services.schema_store import has_elements

router = APIRouter()


# Bursts beyond this are refused before any work; the monthly limit is the plan's.
QUESTIONS_PER_MINUTE = 10


@router.post("/", dependencies=[Depends(rate_limited("question", QUESTIONS_PER_MINUTE))])
async def run_query(payload: QueryRequest, user: CurrentUser, plan: Plan, db: DbSession) -> StreamingResponse:
    """Answer a question about one of the user's connections, streamed as SSE events
    (see app.services.query_pipeline for the event shapes).

    Rejected before streaming, without counting toward the plan, when the connection isn't
    the user's (404) or hasn't been indexed yet (400). Then the question is reserved
    (query_meter): 403 QUERY_LIMIT_REACHED at the monthly limit, 409 while another of the
    user's questions is still running. From here on it counts, whatever the outcome.

    With `clarification`, the request answers a `clarify` event: the original question is
    reopened instead (404 if it expired or isn't the user's) and doesn't count again.
    """
    connection = await connection_service.get_for_user(db, user.id, payload.connection_id)
    if not await has_elements(db, connection.id):
        raise InvalidInput(errors.SCHEMA_NOT_INDEXED)
    if payload.clarification:
        log = await query_meter.resume(db, user.id, connection.id, payload.clarification.question_id)
        log_id, question, clarification = log.id, log.nl_query, payload.clarification.answer
    else:
        log_id = await query_meter.reserve(db, user.id, connection.id, payload.nl_query, plan.max_queries_pm)
        question, clarification = payload.nl_query, None

    async def events() -> AsyncIterator[dict]:
        outcome = PipelineOutcome()
        try:
            async for event in run_pipeline(
                db, connection.id, connection.encrypted_conn_string, question, outcome, clarification
            ):
                if event["type"] == "clarify":
                    event = {**event, "question_id": str(log_id)}  # sent back with the answer
                yield event
        finally:
            # Runs even if the client disconnects mid-answer.
            spawn(query_meter.finish(log_id, outcome), name="finish-question")

    return sse_response(events())


@router.get("/history", response_model=list[QueryLogResponse])
async def get_history(
    user: CurrentUser,
    db: DbSession,
    connection_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[QueryLogResponse]:
    """The user's query history, newest first (questions still running aren't listed)."""
    query = select(QueryLog).where(QueryLog.user_id == user.id, QueryLog.status != "pending")
    if connection_id:
        query = query.where(QueryLog.connection_id == connection_id)
    logs = await db.scalars(
        query.order_by(QueryLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return [QueryLogResponse.model_validate(log) for log in logs]
