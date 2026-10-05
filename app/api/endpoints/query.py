"""Query endpoints — ask a question (SSE stream), mark an answer as verified, read history."""
import time
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
from app.schemas.schemas import (
    AnswerSnapshotResponse,
    QueryHistoryResponse,
    QueryLogResponse,
    QueryRequest,
    VerifiedQueryResponse,
)
from app.services import connections as connection_service
from app.services import answer_snapshot, knowledge, query_meter, question_context, verified_queries
from app.services.query_pipeline import PipelineOutcome, run_pipeline
from app.services.reply_format import parse_reply

router = APIRouter()

# Bursts beyond this are refused before any work; the monthly limit is the plan's.
QUESTIONS_PER_MINUTE = 10
# Events that carry the question's id, so the browser can follow up, verify or clarify.
_EVENTS_WITH_QUESTION_ID = ("results", "clarify", "message")


def _request_started() -> float:
    """When the request reached us; declared first, so it resolves before sign-in and the DB."""
    return time.perf_counter()


@router.post("/", dependencies=[Depends(rate_limited("question", QUESTIONS_PER_MINUTE))])
async def run_query(
    started: Annotated[float, Depends(_request_started)],
    payload: QueryRequest,
    user: CurrentUser,
    plan: Plan,
    db: DbSession,
) -> StreamingResponse:
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

    The same question asked with exactly the same prompt reuses the earlier reply instead of a
    new Gemini call (answer_cache; `results.reused` is true). It still counts. `fresh` asks the
    model again.
    """
    connection = await connection_service.get_for_user(db, user.id, payload.connection_id)
    # Set with the search index in one transaction (schema_indexer), so no extra query is needed.
    if connection.indexed_at is None or not connection.table_count:
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
        # Sign-in, ownership, the meter and context loading, before the pipeline starts.
        outcome.timings["setup"] = int((outcome.started - started) * 1000)
        try:
            async for event in run_pipeline(
                db,
                connection.id,
                connection.encrypted_conn_string,
                question,
                outcome,
                clarification,
                context,
                reuse_for_user=None if payload.fresh else user.id,
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


@router.get("/{question_id}/answer", response_model=AnswerSnapshotResponse)
async def get_saved_answer(question_id: uuid.UUID, user: CurrentUser, db: DbSession) -> AnswerSnapshotResponse:
    """The answer saved when the question was asked, for History. 404 if the question isn't
    the user's or has no saved answer (not answered with rows, or asked before snapshots)."""
    return AnswerSnapshotResponse.model_validate(await answer_snapshot.get(db, user.id, question_id))


@router.get("/history", response_model=QueryHistoryResponse)
async def get_history(
    user: CurrentUser,
    db: DbSession,
    connection_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> QueryHistoryResponse:
    """The user's query history, newest first, with the total for pagination (questions still
    running aren't listed). Each item says whether its answer is verified (👍) and carries the
    model's restatement (`understood`) and the bare statement (`sql`) for display."""
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
    page_logs = list(logs)
    verified = await verified_queries.verified_log_ids(db, user.id, page_logs)
    return QueryHistoryResponse(items=[_history_item(log, log.id in verified) for log in page_logs], total=total)


def _history_item(log: QueryLog, verified: bool) -> QueryLogResponse:
    reply = parse_reply(log.generated_sql or "")
    return QueryLogResponse.model_validate(log).model_copy(
        update={"verified": verified, "understood": reply.understood, "sql": reply.sql or None}
    )
