"""User-safe error messages.

Raw exception text must never reach users: it can expose internal services, hostnames,
or (for malformed URLs) a full connection string with its password. Endpoints and
services translate exceptions with the helpers here and log the detail via
`exception_summary`, which redacts credentials.
"""
import re
from collections.abc import Iterator

import asyncpg.exceptions as pg_errors
from sqlalchemy.exc import ArgumentError

CONNECTION_FAILED = (
    "We could not connect to your database. Check your connection string and "
    "credentials. Ensure your database allows outside connections, then try again."
)
INVALID_CONNECTION_STRING = (
    "That doesn't look like a valid PostgreSQL connection string. "
    "Use the format postgresql://user:password@host:5432/dbname."
)
AUTH_REJECTED = (
    "We could not connect to your database. The username or password was rejected."
)
DATABASE_NOT_FOUND = (
    "We could not connect to your database. The database name in your connection "
    "string does not exist on that server."
)
TOO_MANY_CONNECTIONS = (
    "Your database is not accepting new connections right now (too many open "
    "connections). Please try again in a moment."
)
QUERY_TIMED_OUT = (
    "That took too long. The query ran for more than {seconds} seconds and was "
    "stopped. Try a narrower question, such as a shorter date range."
)
WRITE_PROTECTED = (
    "Write-protected. You can only ask questions here. Operations that modify or "
    "delete data are blocked for your safety."
)
PERMISSION_DENIED = (
    "Your database user doesn't have permission to read that data: {detail}"
)
SQL_FAILED = (
    "The generated SQL failed on your database: {detail}. Try rephrasing your question."
)
SCHEMA_LOOKUP_FAILED = (
    "We couldn't look up your database schema right now. Please try again in a moment."
)
GENERATION_FAILED = (
    "The AI couldn't generate SQL right now. Please try again in a moment."
)
INDEXING_FAILED = (
    "We couldn't finish mapping your database right now. Please try again in a moment."
)
DESIGN_FAILED = (
    "We couldn't generate a schema right now. Please try again in a moment."
)
INTERNAL_ERROR = "Something went wrong on our side. Please try again in a moment."
CANNOT_ANSWER = "That isn't in your data. {reason}Try rephrasing, or ask about something your tables record."
SCHEMA_NOT_INDEXED = (
    "Still mapping your database. We are reading your tables so you can ask questions. "
    "Index this connection and try again in a moment."
)
INDEXING_IN_PROGRESS = "This connection is already being indexed. Please wait for it to finish."
CONNECTION_NOT_FOUND = "Connection not found."
HOST_NOT_ALLOWED = (
    "That database host isn't reachable from QueryMind. Use a database that accepts "
    "connections from the internet (private and local network addresses aren't allowed)."
)
UNSUPPORTED_SCHEME = (
    "Only PostgreSQL connection strings are supported "
    "(postgresql://, postgres://, or postgresql+asyncpg://)."
)

# scheme://user:password@  →  scheme://***@
_DSN_CREDENTIALS = re.compile(r"([a-zA-Z][\w+.-]*://)[^@\s/'\"]+@")


def redact(text: str) -> str:
    """Strip credentials from any connection URL embedded in `text`."""
    return _DSN_CREDENTIALS.sub(r"\1***@", text)


def exception_summary(exc: BaseException) -> str:
    """One-line, credential-free description of an exception, for server logs."""
    return redact(f"{type(exc).__name__}: {exc}")


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield `exc` and its causes (SQLAlchemy's `.orig`, then `__cause__`/`__context__`)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = (
            getattr(current, "orig", None) or current.__cause__ or current.__context__
        )


def _find_postgres_error(exc: BaseException) -> pg_errors.PostgresError | None:
    for item in _exception_chain(exc):
        if isinstance(item, pg_errors.PostgresError):
            return item
    return None


def _postgres_message(error: pg_errors.PostgresError) -> str:
    """The server's primary message only (no SQL text, no connection details)."""
    message = getattr(error, "message", None) or str(error)
    return redact(message.strip().rstrip("."))


def describe_cannot_answer(reason: str) -> str:
    """Message when the model declines because the schema can't answer the question.

    `reason` is the model's one-sentence explanation; it describes the user's own schema.
    """
    reason = " ".join(reason.split())
    if reason and reason[-1] not in ".!?":
        reason += "."
    return CANNOT_ANSWER.format(reason=f"{reason[0].upper()}{reason[1:]} " if reason else "")


def describe_connection_error(exc: BaseException) -> str:
    """Message for a failure while opening a connection to a user's database."""
    if any(isinstance(e, (ArgumentError, ValueError)) for e in _exception_chain(exc)):
        return INVALID_CONNECTION_STRING

    pg_error = _find_postgres_error(exc)
    if pg_error is not None:
        sqlstate = pg_error.sqlstate or ""
        if sqlstate.startswith("28"):
            return AUTH_REJECTED
        if sqlstate == "3D000":
            return DATABASE_NOT_FOUND
        if sqlstate in ("53300", "57P03"):
            return TOO_MANY_CONNECTIONS

    return CONNECTION_FAILED


def describe_query_error(exc: BaseException, timeout_seconds: int) -> str:
    """Message for a failure while running validated SQL on a user's database."""
    pg_error = _find_postgres_error(exc)
    if pg_error is None:
        # No server response at all: the connection itself failed or dropped.
        return describe_connection_error(exc)

    sqlstate = pg_error.sqlstate or ""
    if sqlstate == "57014":
        return QUERY_TIMED_OUT.format(seconds=timeout_seconds)
    if sqlstate == "25006":
        return WRITE_PROTECTED
    if sqlstate == "42501":
        return PERMISSION_DENIED.format(detail=_postgres_message(pg_error))
    if sqlstate.startswith(("08", "28", "3D", "53", "57P")):
        return describe_connection_error(exc)
    # SQL errors (undefined column, syntax, bad cast, ...) are about the user's own
    # query and schema; the server's primary message is safe and genuinely useful.
    return SQL_FAILED.format(detail=_postgres_message(pg_error))
