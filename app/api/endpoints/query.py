"""Query endpoint — SSE pipeline: retrieve schema → stream SQL → validate → execute → log."""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_with_entitlements, Entitlements
from app.core import errors
from app.db.session import get_db
from app.models.models import DBConnection, QueryLog, User
from app.schemas.schemas import QueryRequest
from app.services.query_executor import STATEMENT_TIMEOUT_MS, execute_query
from app.services.schema_retriever import retrieve_schema
from app.services.sql_generator import stream_sql
from app.services.sql_validator import validate_sql

_UNLIMITED = 999_999_999

logger = logging.getLogger(__name__)
router = APIRouter()


def _describe_pipeline_error(exc: Exception, step: str) -> str:
    """Map a pipeline failure to a user-safe message based on the step that failed."""
    if step == "retrieve":
        return errors.SCHEMA_LOOKUP_FAILED
    if step == "generate":
        return errors.GENERATION_FAILED
    if step == "execute":
        return errors.describe_query_error(exc, STATEMENT_TIMEOUT_MS // 1000)
    return errors.INTERNAL_ERROR


def _sse(payload: dict) -> str:
    # Use default=str to handle non-serializable types like datetime, date, UUID
    return f"data: {json.dumps(payload, default=str)}\n\n"


@router.post("/")
async def run_query(
    payload: QueryRequest,
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """
    Full RAG query pipeline streamed via SSE.

    Events:
      {"type": "status", "message": "..."}
      {"type": "sql_chunk", "chunk": "..."}
      {"type": "results", "columns": [...], "rows": [...], "exec_time_ms": N, "row_count": N,
       "truncated": bool}   # truncated: the query had more than MAX_ROWS rows
      {"type": "done"}
      {"type": "error", "message": "..."}  # always user-safe; details go to server logs
    """
    current_user, entitlements = user_and_ent

    # Enforce monthly query limit for non-unlimited plans
    if entitlements.max_queries_pm < _UNLIMITED:
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        monthly_count = await db.scalar(
            select(func.count(QueryLog.id)).where(
                QueryLog.user_id == current_user.id,
                QueryLog.created_at >= month_start,
            )
        )
        if (monthly_count or 0) >= entitlements.max_queries_pm:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="QUERY_LIMIT_REACHED",
            )

    # Validate connection ownership
    result = await db.execute(
        select(DBConnection).where(
            DBConnection.id == payload.connection_id,
            DBConnection.user_id == current_user.id,
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

    if not connection.pinecone_namespace:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Schema not indexed yet. Please index the connection first.",
        )

    async def _pipeline():
        generated_sql = ""
        exec_result = None
        error_msg = None
        status_val = "pending"
        step = "retrieve"

        try:
            # Step 1: Retrieve relevant schema
            yield _sse({"type": "status", "message": "Retrieving schema context..."})
            table_docs = await retrieve_schema(
                payload.nl_query, connection.pinecone_namespace
            )
            known_tables = [doc.table_name for doc in table_docs]

            # Step 2: Stream SQL generation
            step = "generate"
            yield _sse({"type": "status", "message": "Generating SQL..."})
            async for chunk in stream_sql(payload.nl_query, table_docs):
                generated_sql += chunk
                yield _sse({"type": "sql_chunk", "chunk": chunk})

            generated_sql = generated_sql.strip()

            # Step 3: Validate
            step = "validate"
            yield _sse({"type": "status", "message": "Validating SQL..."})
            validation = validate_sql(generated_sql, known_tables=known_tables)
            if not validation.is_valid:
                error_msg = validation.error
                status_val = "validation_error"
                yield _sse({"type": "error", "message": validation.error})
                return

            # Step 4: Execute
            step = "execute"
            yield _sse({"type": "status", "message": "Executing query..."})
            exec_result = await execute_query(
                connection.encrypted_conn_string, generated_sql
            )
            status_val = "success"

            yield _sse(
                {
                    "type": "results",
                    "columns": exec_result.columns,
                    "rows": exec_result.rows,
                    "exec_time_ms": exec_result.exec_time_ms,
                    "row_count": exec_result.row_count,
                    "truncated": exec_result.truncated,
                }
            )
            yield _sse({"type": "done"})

        except Exception as exc:
            # The user (and their history) gets a safe message; logs get the detail.
            error_msg = _describe_pipeline_error(exc, step)
            status_val = "error"
            if step == "execute":
                # Target-DB errors are expected (bad SQL, timeouts); no traceback needed.
                logger.warning(
                    "Query execution failed (connection %s): %s",
                    connection.id,
                    errors.exception_summary(exc),
                )
            else:
                logger.exception("Query pipeline failed at step %r (connection %s)", step, connection.id)
            yield _sse({"type": "error", "message": error_msg})

        finally:
            # Fire-and-forget log — uses its own session (request session is closed by now)
            asyncio.create_task(
                _log_query(
                    user_id=current_user.id,
                    connection_id=connection.id,
                    nl_query=payload.nl_query,
                    generated_sql=generated_sql,
                    exec_result=exec_result,
                    status_val=status_val,
                    error_msg=error_msg,
                )
            )

    return StreamingResponse(
        _pipeline(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _log_query(
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    nl_query: str,
    generated_sql: str,
    exec_result,
    status_val: str,
    error_msg: str | None,
) -> None:
    """Persist query log asynchronously after response is sent.
    Opens a fresh session — the request-scoped session is closed by this point.
    """
    from app.db.session import get_db as _get_db
    try:
        async for db in _get_db():
            log = QueryLog(
                user_id=user_id,
                connection_id=connection_id,
                nl_query=nl_query,
                generated_sql=generated_sql or None,
                row_count=exec_result.row_count if exec_result else None,
                exec_time_ms=exec_result.exec_time_ms if exec_result else None,
                status=status_val,
                error_message=error_msg,
            )
            db.add(log)
            await db.commit()
    except Exception:
        pass  # Logging must never crash the main pipeline


@router.get("/history", response_model=list)
async def get_history(
    connection_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    user_and_ent: tuple[User, Entitlements] = Depends(get_current_user_with_entitlements),
    db: AsyncSession = Depends(get_db),
):
    """Paginated query history for the current user."""
    from app.schemas.schemas import QueryLogResponse

    current_user, _ = user_and_ent
    query = select(QueryLog).where(QueryLog.user_id == current_user.id)
    if connection_id:
        query = query.where(QueryLog.connection_id == connection_id)
    query = (
        query.order_by(QueryLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    logs = result.scalars().all()
    return [QueryLogResponse.model_validate(log) for log in logs]

