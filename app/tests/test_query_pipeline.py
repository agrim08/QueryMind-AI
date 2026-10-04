"""Unit tests for query_pipeline.run_pipeline with retrieval, Gemini and the target DB mocked."""
import asyncio
import uuid

import asyncpg.exceptions as pg_errors
import pytest
from sqlalchemy.exc import DBAPIError

from app.core import errors
from app.services import query_pipeline
from app.services.query_executor import QueryResult
from app.services.query_pipeline import PipelineOutcome, run_pipeline
from app.services.schema_store import TableDoc

INVOICE = TableDoc("invoice", "Table: invoice\nColumns:\n- total (numeric)", 1.0)
CUSTOMER = TableDoc("customer", "Table: customer\nColumns:\n- email (text)", 0.0)
RESULT = QueryResult(columns=["sum"], rows=[[42]], exec_time_ms=3, row_count=1, truncated=False)


def _db_error(pg_error: Exception) -> DBAPIError:
    """Mimic SQLAlchemy's asyncpg dialect: DBAPIError.orig whose __cause__ is the asyncpg error."""
    adapted = Exception("adapted driver error")
    adapted.__cause__ = pg_error
    return DBAPIError("SELECT 1", None, adapted)


class Harness:
    """Scripted model replies and database results; records what the pipeline did."""

    def __init__(self, monkeypatch, replies: list[str], db_results: list[object] | None = None):
        self.replies = list(replies)
        self.db_results = list(db_results or [RESULT])
        self.executed: list[str] = []
        self.prompts: list[tuple[list[str], object]] = []  # (tables shown, feedback) per call
        self.clarifications: list[str | None] = []

        async def retrieve_schema(session, connection_id, question):
            return [INVOICE]

        async def tables_by_name(session, connection_id, names):
            return [CUSTOMER] if "customer" in names else []

        async def all_tables(session, connection_id):
            return [CUSTOMER, INVOICE]

        async def stream_sql(question, docs, feedback=None, clarification=None):
            self.prompts.append(([d.table_name for d in docs], feedback))
            self.clarifications.append(clarification)
            yield self.replies.pop(0)

        async def execute_query(encrypted_url, sql):
            self.executed.append(sql)
            result = self.db_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        for name, fn in [("retrieve_schema", retrieve_schema), ("tables_by_name", tables_by_name),
                         ("all_tables", all_tables), ("stream_sql", stream_sql), ("execute_query", execute_query)]:
            monkeypatch.setattr(query_pipeline, name, fn)

    def run(self, question: str = "total sales?", clarification: str | None = None) -> tuple[list[dict], PipelineOutcome]:
        outcome = PipelineOutcome()

        async def collect() -> list[dict]:
            return [e async for e in run_pipeline(None, uuid.uuid4(), "encrypted", question, outcome, clarification)]

        return asyncio.run(collect()), outcome


def test_fenced_reply_with_assumption_runs_only_the_statement(monkeypatch):
    h = Harness(monkeypatch, ['```sql\n-- Assumption: sales means invoice totals\nSELECT SUM("total") FROM "invoice"\n```'])
    events, outcome = h.run()
    assert h.executed == ['SELECT SUM("total") FROM "invoice"']
    assert outcome.status == "success" and outcome.attempts == 1
    assert [e["type"] for e in events][-2:] == ["results", "done"]
    # History keeps what the model wrote, including its stated assumption.
    assert "Assumption: sales means invoice totals" in outcome.generated_sql


def test_cannot_answer_is_a_friendly_metered_decline(monkeypatch):
    h = Harness(monkeypatch, ["-- Cannot answer: the schema has no refunds table"])
    events, outcome = h.run()
    assert h.executed == [] and len(h.prompts) == 1  # declines are never retried
    assert outcome.status == "validation_error"
    assert outcome.error_message == errors.describe_cannot_answer("the schema has no refunds table")
    assert events[-1] == {"type": "error", "message": outcome.error_message}


def test_unsafe_sql_is_rejected_and_never_retried(monkeypatch):
    h = Harness(monkeypatch, ['-- Assumption: clean up\nDELETE FROM "invoice"'])
    events, outcome = h.run()
    assert h.executed == [] and len(h.prompts) == 1
    assert outcome.status == "validation_error"
    assert events[-1]["type"] == "error"


class TestRetry:
    def test_fixable_database_error_is_retried_once_with_feedback(self, monkeypatch):
        missing_column = _db_error(pg_errors.UndefinedColumnError('column "amount" does not exist'))
        h = Harness(
            monkeypatch,
            ['SELECT SUM("amount") FROM "invoice"', 'SELECT SUM("total") FROM "invoice"'],
            [missing_column, RESULT],
        )
        events, outcome = h.run()
        assert h.executed == ['SELECT SUM("amount") FROM "invoice"', 'SELECT SUM("total") FROM "invoice"']
        assert outcome.status == "success" and outcome.attempts == 2
        assert outcome.generated_sql == 'SELECT SUM("total") FROM "invoice"'
        feedback = h.prompts[1][1]
        assert feedback.previous_sql == 'SELECT SUM("amount") FROM "invoice"'
        assert 'column "amount" does not exist' in feedback.problem
        # The browser is told to discard the first SQL before the new chunks arrive.
        types = [e["type"] for e in events]
        assert types.index("retry") < len(types) - types[::-1].index("sql_chunk") - 1

    def test_only_one_retry(self, monkeypatch):
        missing = _db_error(pg_errors.UndefinedColumnError('column "x" does not exist'))
        h = Harness(monkeypatch, ['SELECT "x" FROM "invoice"', 'SELECT "y" FROM "invoice"'], [missing, missing])
        events, outcome = h.run()
        assert len(h.prompts) == 2 and outcome.status == "error"
        assert outcome.error_message.startswith("The generated SQL failed on your database")

    @pytest.mark.parametrize(
        "pg_error",
        [
            pg_errors.QueryCanceledError("canceling statement due to statement timeout"),
            pg_errors.ReadOnlySQLTransactionError("cannot execute DELETE in a read-only transaction"),
            pg_errors.InsufficientPrivilegeError("permission denied for table invoice"),
        ],
    )
    def test_timeouts_writes_and_permissions_are_not_retried(self, monkeypatch, pg_error):
        h = Harness(monkeypatch, ['SELECT SUM("total") FROM "invoice"'], [_db_error(pg_error)])
        _, outcome = h.run()
        assert len(h.prompts) == 1 and outcome.status == "error"

    def test_a_table_the_model_was_not_shown_is_added_for_the_retry(self, monkeypatch):
        h = Harness(
            monkeypatch,
            ['SELECT "email" FROM "customer"', 'SELECT "c"."email" FROM "customer" AS "c"'],
        )
        _, outcome = h.run()
        assert outcome.status == "success"
        assert h.prompts[0][0] == ["invoice"]
        assert h.prompts[1][0] == ["invoice", "customer"]  # its document is now in the prompt
        assert "customer" in h.prompts[1][1].problem
        assert h.executed == ['SELECT "c"."email" FROM "customer" AS "c"']  # only the validated retry ran


def test_gemini_quota_errors_say_busy(monkeypatch):
    from google.genai import errors as genai_errors

    h = Harness(monkeypatch, [])

    async def refused(question, docs, feedback=None, clarification=None):
        raise genai_errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        yield  # pragma: no cover

    monkeypatch.setattr(query_pipeline, "stream_sql", refused)
    events, outcome = h.run()
    assert outcome.error_message == errors.AI_BUSY
    assert events[-1] == {"type": "error", "message": errors.AI_BUSY}


class TestAnswerKinds:
    def test_results_carry_the_clean_sql_and_a_presentation(self, monkeypatch):
        reply = "\n".join([
            "-- Intent: number",
            "-- Understood: Total sales",
            'SELECT SUM("total") AS "sales" FROM "invoice"',
            "-- Follow-up: How did sales change by month?",
        ])
        result = QueryResult(columns=["sales"], rows=[[2328.6]], exec_time_ms=3, row_count=1, truncated=False)
        h = Harness(monkeypatch, [reply], [result])
        events, _ = h.run()
        results = next(e for e in events if e["type"] == "results")
        assert results["sql"] == 'SELECT SUM("total") AS "sales" FROM "invoice"'
        assert results["answer"]["chart"]["kind"] == "kpi"
        assert results["answer"]["headline"] == "Total sales: 2,328.60"
        assert results["answer"]["follow_ups"] == ["How did sales change by month?"]
        # The header and the suggestions never reach the SQL box.
        streamed = "".join(e["chunk"] for e in events if e["type"] == "sql_chunk")
        assert streamed.strip() == results["sql"]

    def test_clarify_ends_without_running_sql(self, monkeypatch):
        reply = "\n".join([
            "-- Intent: ranking",
            "-- Understood: Best customers",
            "-- Clarify: Best by what?",
            "-- Option: By total spent",
            "-- Option: By number of orders",
        ])
        h = Harness(monkeypatch, [reply])
        events, outcome = h.run("who are our best customers?")
        assert h.executed == []
        assert outcome.status == "clarify" and outcome.kind == "clarify"
        assert events[-2] == {
            "type": "clarify",
            "question": "Best by what?",
            "options": ["By total spent", "By number of orders"],
            "understood": "Best customers",
        }
        assert events[-1] == {"type": "done"}

    def test_the_answer_to_a_clarification_is_passed_to_the_model(self, monkeypatch):
        h = Harness(monkeypatch, ['SELECT "email" FROM "invoice"'])
        _, outcome = h.run("who are our best customers?", clarification="By total spent")
        assert h.clarifications == ["By total spent"] and outcome.status == "success"

    def test_only_one_clarification_round(self, monkeypatch):
        h = Harness(monkeypatch, ["-- Clarify: Best by what?\n-- Option: A\n-- Option: B"])
        events, outcome = h.run("best customers?", clarification="By total spent")
        assert outcome.status == "validation_error"
        assert events[-1] == {"type": "error", "message": errors.CLARIFY_AGAIN}

    def test_schema_answer_is_a_message(self, monkeypatch):
        h = Harness(monkeypatch, ["-- Intent: schema\n-- Answer: Customer emails are in customer.email."])
        events, outcome = h.run("where are customer emails stored?")
        assert h.executed == [] and outcome.status == "success"
        assert {"type": "message", "text": "Customer emails are in customer.email."} in events

    def test_write_requests_are_refused_with_guidance(self, monkeypatch):
        h = Harness(monkeypatch, ["-- Intent: other\n-- Not allowed: delete inactive users"])
        events, outcome = h.run("delete inactive users")
        assert h.executed == [] and outcome.status == "validation_error"
        assert events[-1] == {"type": "error", "message": errors.WRITE_REQUEST}

    def test_table_listing_needs_no_ai_call(self, monkeypatch):
        h = Harness(monkeypatch, [])
        events, outcome = h.run("What tables do I have?")
        assert h.prompts == [] and outcome.status == "success"
        message = next(e for e in events if e["type"] == "message")
        assert "2 tables and views: customer, invoice" in message["text"]
