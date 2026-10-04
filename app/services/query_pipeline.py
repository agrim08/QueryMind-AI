"""Question → answer pipeline: retrieve schema → generate SQL → validate → execute.

`run_pipeline` yields the events streamed to the browser:
  {"type": "status",    "message": "..."}
  {"type": "sql_chunk", "chunk": "..."}
  {"type": "results",   "columns": [...], "rows": [...], "exec_time_ms": N,
                        "row_count": N, "truncated": bool}
  {"type": "done"}
  {"type": "error",     "message": "..."}   # always user-safe; details go to logs

It records what happened in a `PipelineOutcome`, which the caller persists as the
QueryLog row (the usage meter) once the stream ends.
"""
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.db.session import AsyncSessionLocal
from app.models.models import QueryLog
from app.services.query_executor import STATEMENT_TIMEOUT_MS, QueryResult, execute_query
from app.services.schema_retriever import retrieve_schema
from app.services.sql_generator import stream_sql
from app.services.sql_validator import validate_sql

logger = logging.getLogger(__name__)

Step = Literal["retrieve", "generate", "validate", "execute"]


@dataclass
class PipelineOutcome:
    generated_sql: str = ""
    status: Literal["pending", "success", "validation_error", "error"] = "pending"
    error_message: str | None = None
    result: QueryResult | None = None


def describe_failure(exc: Exception, step: Step) -> str:
    """User-safe message for a failure, based on the step that failed."""
    if step == "retrieve":
        return errors.SCHEMA_LOOKUP_FAILED
    if step == "generate":
        return errors.GENERATION_FAILED
    if step == "execute":
        return errors.describe_query_error(exc, STATEMENT_TIMEOUT_MS // 1000)
    return errors.INTERNAL_ERROR


async def run_pipeline(
    session: AsyncSession,
    connection_id: uuid.UUID,
    encrypted_url: str,
    question: str,
    outcome: PipelineOutcome,
) -> AsyncIterator[dict]:
    step: Step = "retrieve"
    try:
        yield {"type": "status", "message": "Retrieving schema context..."}
        table_docs = await retrieve_schema(session, connection_id, question)

        step = "generate"
        yield {"type": "status", "message": "Generating SQL..."}
        async for chunk in stream_sql(question, table_docs):
            outcome.generated_sql += chunk
            yield {"type": "sql_chunk", "chunk": chunk}
        outcome.generated_sql = outcome.generated_sql.strip()

        step = "validate"
        yield {"type": "status", "message": "Validating SQL..."}
        validation = validate_sql(outcome.generated_sql, known_tables=[d.table_name for d in table_docs])
        if not validation.is_valid:
            outcome.status, outcome.error_message = "validation_error", validation.error
            yield {"type": "error", "message": validation.error}
            return

        step = "execute"
        yield {"type": "status", "message": "Executing query..."}
        result = await execute_query(encrypted_url, outcome.generated_sql)
        outcome.status, outcome.result = "success", result
        yield {
            "type": "results",
            "columns": result.columns,
            "rows": result.rows,
            "exec_time_ms": result.exec_time_ms,
            "row_count": result.row_count,
            "truncated": result.truncated,
        }
        yield {"type": "done"}

    except Exception as exc:
        outcome.status, outcome.error_message = "error", describe_failure(exc, step)
        if step == "execute":
            # Target-DB errors are expected (bad SQL, timeouts); no traceback needed.
            logger.warning(
                "Query execution failed (connection %s): %s", connection_id, errors.exception_summary(exc)
            )
        else:
            logger.exception("Query pipeline failed at step %r (connection %s)", step, connection_id)
        yield {"type": "error", "message": outcome.error_message}


async def record_query(
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    question: str,
    outcome: PipelineOutcome,
) -> None:
    """Persist the QueryLog row (history + usage meter) in its own session."""
    async with AsyncSessionLocal() as session:
        session.add(
            QueryLog(
                user_id=user_id,
                connection_id=connection_id,
                nl_query=question,
                generated_sql=outcome.generated_sql or None,
                row_count=outcome.result.row_count if outcome.result else None,
                exec_time_ms=outcome.result.exec_time_ms if outcome.result else None,
                status=outcome.status,
                error_message=outcome.error_message,
            )
        )
        await session.commit()
