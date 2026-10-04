"""Unit tests for app.core.errors — user-safe error mapping and credential redaction."""
import asyncpg.exceptions as pg_errors
from sqlalchemy.exc import ArgumentError, DBAPIError

from app.core import errors


def _wrapped(pg_error: Exception) -> DBAPIError:
    """Mimic SQLAlchemy's asyncpg dialect: DBAPIError.orig whose __cause__ is the asyncpg error."""
    adapted = Exception("adapted driver error")
    adapted.__cause__ = pg_error
    return DBAPIError("SELECT 1", None, adapted)


def _pg(cls: type[pg_errors.PostgresError], message: str) -> pg_errors.PostgresError:
    error = cls(message)
    error.message = message
    return error


class TestRedact:
    def test_strips_password_from_url(self):
        text = "Could not parse 'postgresql+asyncpg://bob:s3cret@db.example.com:5432/app'"
        redacted = errors.redact(text)
        assert "s3cret" not in redacted
        assert "bob" not in redacted
        assert "postgresql+asyncpg://***@db.example.com:5432/app" in redacted

    def test_leaves_plain_text_alone(self):
        assert errors.redact('column "email" does not exist') == 'column "email" does not exist'

    def test_exception_summary_is_redacted(self):
        exc = ArgumentError("Could not parse URL from string 'postgres://u:pw@h/db'")
        summary = errors.exception_summary(exc)
        assert summary.startswith("ArgumentError:")
        assert "pw" not in summary


class TestDescribeConnectionError:
    def test_malformed_url(self):
        exc = ArgumentError("Could not parse SQLAlchemy URL from string 'postgres://u:pw@'")
        message = errors.describe_connection_error(exc)
        assert message == errors.INVALID_CONNECTION_STRING
        assert "pw" not in message

    def test_bad_password(self):
        exc = _wrapped(_pg(pg_errors.InvalidPasswordError, 'password authentication failed for user "bob"'))
        assert errors.describe_connection_error(exc) == errors.AUTH_REJECTED

    def test_missing_database(self):
        exc = _wrapped(_pg(pg_errors.InvalidCatalogNameError, 'database "nope" does not exist'))
        assert errors.describe_connection_error(exc) == errors.DATABASE_NOT_FOUND

    def test_unreachable_host_falls_back_to_brand_copy(self):
        exc = OSError("[Errno 11001] getaddrinfo failed for internal-host.local")
        message = errors.describe_connection_error(exc)
        assert message == errors.CONNECTION_FAILED
        assert "internal-host" not in message


class TestDescribeQueryError:
    def test_timeout(self):
        exc = _wrapped(_pg(pg_errors.QueryCanceledError, "canceling statement due to statement timeout"))
        assert errors.describe_query_error(exc, 10) == errors.QUERY_TIMED_OUT.format(seconds=10)

    def test_read_only_violation_is_write_protected(self):
        exc = _wrapped(_pg(pg_errors.ReadOnlySQLTransactionError, "cannot execute INSERT in a read-only transaction"))
        assert errors.describe_query_error(exc, 10) == errors.WRITE_PROTECTED

    def test_sql_error_keeps_postgres_primary_message(self):
        exc = _wrapped(_pg(pg_errors.UndefinedColumnError, 'column "emial" does not exist'))
        message = errors.describe_query_error(exc, 10)
        assert 'column "emial" does not exist' in message
        assert "SELECT 1" not in message  # never echo the statement / driver wrapper

    def test_permission_denied(self):
        exc = _wrapped(_pg(pg_errors.InsufficientPrivilegeError, "permission denied for table salaries"))
        assert "permission denied for table salaries" in errors.describe_query_error(exc, 10)

    def test_connection_drop_during_query(self):
        assert errors.describe_query_error(ConnectionResetError("reset by peer"), 10) == errors.CONNECTION_FAILED

    def test_unknown_internal_error_never_leaks_text(self):
        exc = RuntimeError("pinecone index qm-prod-7 at https://internal.svc returned 500")
        message = errors.describe_query_error(exc, 10)
        assert "pinecone" not in message.lower()
        assert "internal.svc" not in message


class TestCannotAnswer:
    def test_reason_is_capitalised_and_punctuated(self):
        assert errors.describe_cannot_answer("artists have no phone column") == (
            "That isn't in your data. Artists have no phone column. "
            "Try rephrasing, or ask about something your tables record."
        )

    def test_without_a_reason(self):
        assert errors.describe_cannot_answer("  ") == (
            "That isn't in your data. Try rephrasing, or ask about something your tables record."
        )
