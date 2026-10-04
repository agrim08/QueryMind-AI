"""Question → answer pipeline: retrieve schema → generate → validate → execute → present.

`run_pipeline` yields the events streamed to the browser:
  {"type": "status",    "message": "..."}
  {"type": "sql_chunk", "chunk": "..."}       # only the SQL statement, never the reply's metadata
  {"type": "retry",     "message": "..."}     # the SQL so far is discarded; new chunks follow
  {"type": "results",   "sql": "...", "columns": [...], "rows": [...], "exec_time_ms": N,
                        "row_count": N, "truncated": bool,
                        "answer": {intent, understood, assumptions, alternatives, follow_ups,
                                   headline, chart}}        # see answer_presentation
  {"type": "clarify",   "question": "...", "options": [...], "understood": "..."}
  {"type": "message",   "text": "..."}        # a plain answer about the database itself
  {"type": "done"}
  {"type": "error",     "message": "..."}     # always user-safe; details go to logs

Every question costs at most one Gemini call, plus one automatic retry (the only extra call,
and only on failure) when the first query can be fixed: the database rejected it as a query
mistake, or it used a table the model wasn't shown, which is then added if it exists. Write
attempts, permission errors, timeouts and declines are never retried. "What tables do I
have?" is answered from the index with no Gemini call at all.

What happened is recorded in a `PipelineOutcome`, which the caller writes to the question's
QueryLog row (see query_meter) when the stream ends.
"""
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.services import schema_answers
from app.services.answer_presentation import Presentation, present
from app.services.query_executor import STATEMENT_TIMEOUT_MS, QueryResult, execute_query
from app.services.reply_format import SqlReply, SqlStreamFilter, parse_reply
from app.services.schema_retriever import retrieve_schema
from app.services.schema_store import all_tables, tables_by_name
from app.services.sql_generator import RetryFeedback, stream_sql
from app.services.sql_validator import validate_sql

logger = logging.getLogger(__name__)

Step = Literal["retrieve", "generate", "validate", "execute"]
AnswerKind = Literal["rows", "clarify", "message", "not_allowed", "declined"]

MAX_ATTEMPTS = 2  # the first answer plus one retry


@dataclass
class PipelineOutcome:
    generated_sql: str = ""  # the final attempt's full reply
    status: Literal["pending", "success", "validation_error", "error", "clarify"] = "pending"
    error_message: str | None = None
    result: QueryResult | None = None
    attempts: int = 0
    kind: AnswerKind | None = None
    reply: SqlReply | None = None


def describe_failure(exc: Exception, step: Step) -> str:
    """User-safe message for a failure, based on the step that failed."""
    if errors.is_ai_rate_limited(exc):
        return errors.AI_BUSY
    if step == "retrieve":
        return errors.SCHEMA_LOOKUP_FAILED
    if step == "generate":
        return errors.GENERATION_FAILED
    if step == "execute":
        return errors.describe_query_error(exc, STATEMENT_TIMEOUT_MS // 1000)
    return errors.INTERNAL_ERROR


def _presentation(reply: SqlReply, result: QueryResult) -> Presentation:
    """Chart and headline for the result; presentation must never fail an answer."""
    try:
        return present(reply, result.columns, result.rows, result.truncated)
    except Exception:
        logger.exception("Answer presentation failed; falling back to a table")
        return Presentation(
            intent=reply.intent,
            understood=reply.understood,
            assumptions=list(reply.assumptions),
            alternatives=list(reply.alternatives),
            follow_ups=list(reply.follow_ups),
            headline=f"{result.row_count:,} rows.",
        )


async def run_pipeline(
    session: AsyncSession,
    connection_id: uuid.UUID,
    encrypted_url: str,
    question: str,
    outcome: PipelineOutcome,
    clarification: str | None = None,
) -> AsyncIterator[dict]:
    """Answer `question`; `clarification` is the user's answer to a clarifying question."""
    step: Step = "retrieve"
    try:
        if clarification is None and schema_answers.is_table_listing(question):
            yield {"type": "status", "message": "Reading your schema..."}
            docs = await all_tables(session, connection_id)
            outcome.status, outcome.kind = "success", "message"
            yield {"type": "message", "text": schema_answers.describe_tables([d.table_name for d in docs])}
            yield {"type": "done"}
            return

        yield {"type": "status", "message": "Retrieving schema context..."}
        table_docs = await retrieve_schema(session, connection_id, question)

        feedback: RetryFeedback | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            outcome.attempts = attempt
            step = "generate"
            if feedback is None:
                yield {"type": "status", "message": "Generating SQL..."}
            else:
                yield {"type": "retry", "message": "Fixing the query..."}
            outcome.generated_sql = ""
            shown = SqlStreamFilter()
            async for chunk in stream_sql(question, table_docs, feedback, clarification):
                outcome.generated_sql += chunk
                if sql_text := shown.feed(chunk):
                    yield {"type": "sql_chunk", "chunk": sql_text}
            if sql_text := shown.finish():
                yield {"type": "sql_chunk", "chunk": sql_text}
            outcome.generated_sql = outcome.generated_sql.strip()
            reply = outcome.reply = parse_reply(outcome.generated_sql)

            step = "validate"
            if reply.cannot_answer is not None:
                # Still a metered query (business-logic.md §5), shown as a friendly message.
                outcome.kind, outcome.status = "declined", "validation_error"
                outcome.error_message = errors.describe_cannot_answer(reply.cannot_answer)
                yield {"type": "error", "message": outcome.error_message}
                return
            if reply.not_allowed is not None:
                outcome.kind, outcome.status = "not_allowed", "validation_error"
                outcome.error_message = errors.WRITE_REQUEST
                yield {"type": "error", "message": errors.WRITE_REQUEST}
                return
            if reply.clarify is not None:
                if clarification is not None:  # at most one clarification round
                    outcome.kind, outcome.status = "declined", "validation_error"
                    outcome.error_message = errors.CLARIFY_AGAIN
                    yield {"type": "error", "message": errors.CLARIFY_AGAIN}
                    return
                outcome.kind, outcome.status = "clarify", "clarify"
                yield {
                    "type": "clarify",
                    "question": reply.clarify.question,
                    "options": list(reply.clarify.options),
                    "understood": reply.understood,
                }
                yield {"type": "done"}
                return
            if reply.answer is not None:
                outcome.kind, outcome.status = "message", "success"
                yield {"type": "message", "text": reply.answer}
                yield {"type": "done"}
                return

            yield {"type": "status", "message": "Validating SQL..."}
            # The statement validated is exactly the statement executed.
            validation = validate_sql(reply.sql, known_tables=[d.table_name for d in table_docs])
            if not validation.is_valid:
                if attempt < MAX_ATTEMPTS and validation.unknown_tables:
                    # A table the model wasn't shown: add it if it exists, then retry once.
                    table_docs = table_docs + await tables_by_name(
                        session, connection_id, set(validation.unknown_tables)
                    )
                    feedback = RetryFeedback(reply.sql, validation.error or "")
                    continue
                outcome.status, outcome.error_message = "validation_error", validation.error
                yield {"type": "error", "message": validation.error}
                return

            step = "execute"
            yield {"type": "status", "message": "Executing query..."}
            try:
                result = await execute_query(encrypted_url, reply.sql)
            except Exception as exc:
                problem = errors.fixable_sql_error(exc)
                if attempt < MAX_ATTEMPTS and problem:
                    logger.info("Retrying a fixable query error (connection %s): %s", connection_id, problem)
                    feedback = RetryFeedback(reply.sql, problem)
                    continue
                raise

            outcome.kind, outcome.status, outcome.result = "rows", "success", result
            yield {
                "type": "results",
                "sql": reply.sql,
                "columns": result.columns,
                "rows": result.rows,
                "exec_time_ms": result.exec_time_ms,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "answer": _presentation(reply, result).to_dict(),
            }
            yield {"type": "done"}
            return

    except Exception as exc:
        outcome.status, outcome.error_message = "error", describe_failure(exc, step)
        if step == "execute":
            # Target-DB errors are expected (bad SQL, timeouts); no traceback needed.
            logger.warning(
                "Query execution failed (connection %s): %s", connection_id, errors.exception_summary(exc)
            )
        elif errors.is_ai_rate_limited(exc):
            logger.warning("Gemini rate limit at step %r (connection %s)", step, connection_id)
        else:
            logger.exception("Query pipeline failed at step %r (connection %s)", step, connection_id)
        yield {"type": "error", "message": outcome.error_message}
