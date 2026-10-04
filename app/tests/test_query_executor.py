"""Tests for query_executor — read-only transaction, row cap, and cleanup.

The unit tests use a fake engine and always run. The integration tests run real
SQL against a Postgres you point them at, and are skipped otherwise:

    QM_TEST_TARGET_DATABASE_URL=postgresql://user:pass@localhost:5432/scratch pytest -q

Use a throwaway database: the tests prove writes are rejected, so nothing is written.
"""
import asyncio
import os
from contextlib import asynccontextmanager

import asyncpg.exceptions as pg_errors
import pytest

from app.core import errors
from app.core.config import settings
from app.services import query_executor
from app.services.query_executor import MAX_ROWS, execute_query
from app.services.target_db import normalize_url


# ── Fake engine (unit tests) ──────────────────────────────────────────────────

class _FakeStreamResult:
    def __init__(self, total_rows: int):
        self._rows = [(i, f"row-{i}") for i in range(total_rows)]
        self.requested: int | None = None
        self.closed = False

    def keys(self):
        return ["id", "label"]

    async def fetchmany(self, size: int):
        self.requested = size
        return self._rows[:size]

    async def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, total_rows: int):
        self.executed: list[str] = []
        self.stream_result = _FakeStreamResult(total_rows)
        self.streamed_sql: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.executed.append(str(statement))

    async def stream(self, statement):
        self.streamed_sql = str(statement)
        return self.stream_result


class _FakeEngine:
    def __init__(self, total_rows: int):
        self.connection = _FakeConnection(total_rows)
        self.disposed = False

    def connect(self):
        return self.connection

    async def dispose(self):
        self.disposed = True


@pytest.fixture
def fake_engine(monkeypatch):
    def install(total_rows: int) -> _FakeEngine:
        engine = _FakeEngine(total_rows)

        @asynccontextmanager
        async def fake_open_target_engine(url):
            try:
                yield engine
            finally:
                await engine.dispose()

        monkeypatch.setattr(query_executor, "decrypt_url", lambda token: token)
        monkeypatch.setattr(query_executor, "open_target_engine", fake_open_target_engine)
        return engine

    return install


class TestExecutorUnit:
    def test_read_only_transaction_is_set_before_the_query(self, fake_engine):
        engine = fake_engine(3)
        asyncio.run(execute_query("postgresql://u:p@h/db", "SELECT 1"))

        executed = engine.connection.executed
        assert executed[0] == "SET TRANSACTION READ ONLY"
        assert executed[1].startswith("SET LOCAL statement_timeout")
        assert engine.connection.streamed_sql == "SELECT 1"

    def test_only_max_rows_plus_one_are_fetched(self, fake_engine):
        engine = fake_engine(50_000)
        result = asyncio.run(execute_query("postgresql://u:p@h/db", "SELECT * FROM big"))

        assert engine.connection.stream_result.requested == MAX_ROWS + 1
        assert result.row_count == MAX_ROWS
        assert len(result.rows) == MAX_ROWS
        assert result.truncated is True

    def test_small_result_is_not_truncated(self, fake_engine):
        fake_engine(7)
        result = asyncio.run(execute_query("postgresql://u:p@h/db", "SELECT * FROM small"))

        assert result.row_count == 7
        assert result.truncated is False
        assert result.columns == ["id", "label"]

    def test_exactly_max_rows_is_not_truncated(self, fake_engine):
        fake_engine(MAX_ROWS)
        result = asyncio.run(execute_query("postgresql://u:p@h/db", "SELECT * FROM t"))
        assert result.row_count == MAX_ROWS
        assert result.truncated is False

    def test_engine_disposed_and_cursor_closed(self, fake_engine):
        engine = fake_engine(1)
        asyncio.run(execute_query("postgresql://u:p@h/db", "SELECT 1"))
        assert engine.disposed
        assert engine.connection.stream_result.closed

    def test_engine_disposed_when_query_fails(self, fake_engine):
        engine = fake_engine(1)

        async def boom(statement):
            raise RuntimeError("query failed")

        engine.connection.stream = boom
        with pytest.raises(RuntimeError):
            asyncio.run(execute_query("postgresql://u:p@h/db", "SELECT 1"))
        assert engine.disposed


# ── Real Postgres (integration tests, opt-in) ─────────────────────────────────

TARGET_URL = os.environ.get("QM_TEST_TARGET_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TARGET_URL, reason="set QM_TEST_TARGET_DATABASE_URL to run against a real Postgres"
)


@pytest.fixture
def plain_decrypt(monkeypatch):
    """Use the URL as given (no encryption) and allow the local test host."""
    monkeypatch.setattr(query_executor, "decrypt_url", normalize_url)
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_DB_HOSTS", True)


def _run(sql: str):
    return asyncio.run(execute_query(TARGET_URL, sql))


def _sqlstate(exc: BaseException) -> str | None:
    pg_error = errors._find_postgres_error(exc)
    return pg_error.sqlstate if pg_error else None


@requires_db
class TestExecutorIntegration:
    def test_select_into_is_rejected_by_postgres(self, plain_decrypt):
        with pytest.raises(Exception) as info:
            _run("SELECT * INTO qm_should_not_exist FROM generate_series(1, 3)")
        assert _sqlstate(info.value) == pg_errors.ReadOnlySQLTransactionError.sqlstate
        assert errors.describe_query_error(info.value, 10) == errors.WRITE_PROTECTED

    def test_select_into_temp_is_rejected_by_postgres(self, plain_decrypt):
        # Even temporary tables cannot be created inside a READ ONLY transaction.
        with pytest.raises(Exception) as info:
            _run("WITH x AS (SELECT 1) SELECT * INTO TEMP qm_tmp FROM x")
        assert _sqlstate(info.value) == pg_errors.ReadOnlySQLTransactionError.sqlstate

    def test_row_cap_on_large_result(self, plain_decrypt):
        result = _run("SELECT g FROM generate_series(1, 100000) AS g")
        assert result.row_count == MAX_ROWS
        assert result.truncated is True
        assert result.rows[0] == [1]

    def test_small_result(self, plain_decrypt):
        result = _run("SELECT g FROM generate_series(1, 3) AS g")
        assert result.rows == [[1], [2], [3]]
        assert result.truncated is False

    def test_statement_timeout(self, plain_decrypt, monkeypatch):
        monkeypatch.setattr(query_executor, "STATEMENT_TIMEOUT_MS", 300)
        with pytest.raises(Exception) as info:
            _run("SELECT pg_sleep(2)")
        assert _sqlstate(info.value) == pg_errors.QueryCanceledError.sqlstate
