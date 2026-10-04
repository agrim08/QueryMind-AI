"""Query Executor — runs a validated SELECT on the user's database.

Key safety guarantees:
- Fresh async connection per request (no shared pool with user DBs).
- Every query runs inside a READ ONLY transaction, so Postgres itself rejects
  writes, DDL, SELECT ... INTO, FOR UPDATE/SHARE and nextval(), whatever the SQL says.
  The transaction is never committed; it is rolled back when the connection closes.
- statement_timeout is set for that transaction only (SET LOCAL), so it also works
  behind transaction-mode poolers such as Neon's.
- Rows are read through a server-side cursor, so at most MAX_ROWS + 1 rows ever
  leave the database, no matter how large the result is.
- Connection is always closed after execution, and the outbound-host policy is
  enforced on every run (see app.services.target_db).
"""
import time
from dataclasses import dataclass

from sqlalchemy import text

from app.services.target_db import decrypt_url, open_target_engine

MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 10_000  # 10 seconds


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    exec_time_ms: int
    row_count: int
    truncated: bool  # True when the query had more than MAX_ROWS rows


async def execute_query(encrypted_conn_string: str, sql: str) -> QueryResult:
    """
    Execute a validated SELECT query on the user's database.

    Args:
        encrypted_conn_string: Fernet-encrypted connection string from DB.
        sql: Validated SELECT SQL to execute.

    Returns:
        QueryResult with columns, at most MAX_ROWS rows, timing, and a truncation flag.

    Raises:
        Exception: Propagates DB errors to the caller, which maps them to a
        user-safe message with `app.core.errors.describe_query_error`.
    """
    # Fresh engine per request — no persistent pool for user DBs.
    async with open_target_engine(decrypt_url(encrypted_conn_string)) as engine, engine.connect() as conn:
        # Both settings are scoped to this transaction, which is never committed.
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))

        start = time.perf_counter()
        result = await conn.stream(text(sql))
        columns = list(result.keys())
        # Fetch one extra row to detect truncation without reading the rest.
        fetched = await result.fetchmany(MAX_ROWS + 1)
        await result.close()
        elapsed_ms = int((time.perf_counter() - start) * 1000)

    truncated = len(fetched) > MAX_ROWS
    rows = [list(row) for row in fetched[:MAX_ROWS]]
    return QueryResult(
        columns=columns,
        rows=rows,
        exec_time_ms=elapsed_ms,
        row_count=len(rows),
        truncated=truncated,
    )
