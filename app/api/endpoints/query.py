"""Query endpoints — ask a question (SSE stream), mark an answer as verified, read history."""
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import CurrentUser, Plan, rate_limited
from app.core import errors
from app.core.background import spawn
from app.core.exceptions import InvalidInput
from app.core.sse import sse_response
from app.db.session import DbSession
from app.models.models import QueryLog
from app.schemas.schemas import QueryHistoryResponse, QueryLogResponse, QueryRequest, VerifiedQueryResponse
from app.services import connections as connection_service
from app.services import knowledge, query_meter, question_context, verified_queries
from app.services.query_pipeline import PipelineOutcome, run_pipeline
from app.services.schema_store import has_elements

router = APIRouter()

# Bursts beyond this are refused before any work; the monthly limit is the plan's.
QUESTIONS_PER_MINUTE = 10
# Events that carry the question's id, so the browser can follow up, verify or clarify.
_EVENTS_WITH_QUESTION_ID = ("results", "clarify", "message")


@router.post("/", dependencies=[Depends(rate_limited("question", QUESTIONS_PER_MINUTE))])
async def run_query(payload: QueryRequest, user: CurrentUser, plan: Plan, db: DbSession) -> StreamingResponse:
    """Answer a question about one of the user's connections, streamed as SSE events
    (see app.services.query_pipeline for the event shapes; `results`, `clarify` and `message`
    also carry `question_id`).

    Rejected before streaming, without counting toward the plan, when the connection isn't
    the user's (404) or hasn't been indexed yet (400). Then the question is reserved
    (query_meter): 403 QUERY_LIMIT_REACHED at the monthly limit, 409 while another of the
    user's questions is still running. From here on it counts, whatever the outcome.

    With `clarification`, the request answers a `clarify` event: the original question is
    reopened instead (404 if it expired or isn't the user's), doesn't count again, and the
    answer is remembered so a similar question isn't asked twice. With `follow_up_of`, the
    earlier question and its SQL are given to the model as conversation context.
    """
    connection = await connection_service.get_for_user(db, user.id, payload.connection_id)
    if not await has_elements(db, connection.id):
        raise InvalidInput(errors.SCHEMA_NOT_INDEXED)
    if payload.clarification:
        log = await query_meter.resume(db, user.id, connection.id, payload.clarification.question_id)
        log_id, question, follow_up_of = log.id, log.nl_query, log.follow_up_of
        clarification = payload.clarification.answer
        await knowledge.remember_clarification(db, user.id, log, clarification)
    else:
        log_id = await query_meter.reserve(
            db, user.id, connection.id, payload.nl_query, plan.max_queries_pm, payload.follow_up_of
        )
        question, clarification, follow_up_of = payload.nl_query, None, payload.follow_up_of
    context = await question_context.load(db, user.id, connection.id, question, follow_up_of)

    async def events() -> AsyncIterator[dict]:
        outcome = PipelineOutcome()
        try:
            async for event in run_pipeline(
                db, connection.id, connection.encrypted_conn_string, question, outcome, clarification, context
            ):
                if event["type"] in _EVENTS_WITH_QUESTION_ID:
                    event = {**event, "question_id": str(log_id)}
                yield event
        finally:
            # Runs even if the client disconnects mid-answer.
            spawn(query_meter.finish(log_id, outcome), name="finish-question")

    return sse_response(events())


@router.post("/{question_id}/verify", response_model=VerifiedQueryResponse)
async def verify_answer(question_id: uuid.UUID, user: CurrentUser, db: DbSession) -> VerifiedQueryResponse:
    """Mark an answered question as correct (👍). Similar questions then get it as a worked
    example. 404 if the question isn't the user's or wasn't answered with SQL."""
    return VerifiedQueryResponse.model_validate(await verified_queries.verify(db, user.id, question_id))


@router.get("/history", response_model=QueryHistoryResponse)
async def get_history(
    user: CurrentUser,
    db: DbSession,
    connection_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> QueryHistoryResponse:
    """The user's query history, newest first, with the total for pagination (questions still
    running aren't listed)."""
    filters = [QueryLog.user_id == user.id, QueryLog.status != "pending"]
    if connection_id:
        filters.append(QueryLog.connection_id == connection_id)
    total = await db.scalar(select(func.count(QueryLog.id)).where(*filters)) or 0
    logs = await db.scalars(
        select(QueryLog)
        .where(*filters)
        .order_by(QueryLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return QueryHistoryResponse(items=[QueryLogResponse.model_validate(log) for log in logs], total=total)
