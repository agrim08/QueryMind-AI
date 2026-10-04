"""Unit tests for query_pipeline.run_pipeline with retrieval, Gemini and the target DB mocked."""
import asyncio
import uuid

import pytest

from app.core import errors
from app.services import query_pipeline
from app.services.query_executor import QueryResult
from app.services.query_pipeline import PipelineOutcome, run_pipeline
from app.services.schema_store import TableDoc

DOCS = [TableDoc("invoice", "Table: invoice\nColumns:\n- total (NUMERIC)", 0.0)]


@pytest.fixture
def executed(monkeypatch) -> list[str]:
    """Mocks the pipeline's collaborators; returns the SQL statements sent to the executor."""
    sent: list[str] = []

    async def retrieve_schema(session, connection_id, question):
        return DOCS

    async def execute_query(encrypted_url, sql):
        sent.append(sql)
        return QueryResult(columns=["sum"], rows=[[42]], exec_time_ms=3, row_count=1, truncated=False)

    monkeypatch.setattr(query_pipeline, "retrieve_schema", retrieve_schema)
    monkeypatch.setattr(query_pipeline, "execute_query", execute_query)
    return sent


def _run(monkeypatch, model_reply: str) -> tuple[list[dict], PipelineOutcome]:
    async def stream_sql(question, docs):
        yield model_reply

    monkeypatch.setattr(query_pipeline, "stream_sql", stream_sql)
    outcome = PipelineOutcome()

    async def collect() -> list[dict]:
        return [e async for e in run_pipeline(None, uuid.uuid4(), "encrypted", "total sales?", outcome)]

    return asyncio.run(collect()), outcome


def test_fenced_reply_with_assumption_runs_only_the_statement(monkeypatch, executed):
    events, outcome = _run(
        monkeypatch, '```sql\n-- Assumption: sales means invoice totals\nSELECT SUM("total") FROM "invoice"\n```'
    )
    assert executed == ['SELECT SUM("total") FROM "invoice"']
    assert outcome.status == "success"
    assert [e["type"] for e in events][-2:] == ["results", "done"]
    # History keeps what the model wrote, including its stated assumption.
    assert "Assumption: sales means invoice totals" in outcome.generated_sql


def test_cannot_answer_is_a_friendly_metered_decline(monkeypatch, executed):
    events, outcome = _run(monkeypatch, "-- Cannot answer: the schema has no refunds table")
    assert executed == []
    assert outcome.status == "validation_error"
    assert outcome.error_message == errors.describe_cannot_answer("the schema has no refunds table")
    assert events[-1] == {"type": "error", "message": outcome.error_message}


def test_unsafe_sql_is_still_rejected_before_execution(monkeypatch, executed):
    events, outcome = _run(monkeypatch, "-- Assumption: clean up\nDELETE FROM \"invoice\"")
    assert executed == []
    assert outcome.status == "validation_error"
    assert events[-1]["type"] == "error"
