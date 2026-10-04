"""Everything about connecting to a user's own (target) Postgres database.

The single place that:
- normalises connection URLs to the asyncpg driver,
- enforces the outbound-host policy (SSRF guard),
- opens short-lived engines (no shared pools across tenants) and disposes them.

Used by the connections endpoints, the schema indexer and the query executor.
"""
import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import errors
from app.core.config import settings
from app.core.exceptions import InvalidInput
from app.core.security import decrypt

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10

_SCHEME_REWRITES = (
    ("postgresql+asyncpg://", "postgresql+asyncpg://"),
    ("postgresql+psycopg2://", "postgresql+asyncpg://"),
    ("postgresql+psycopg://", "postgresql+asyncpg://"),
    ("postgresql://", "postgresql+asyncpg://"),
    ("postgres://", "postgresql+asyncpg://"),
)

# libpq / psycopg2 query parameters that asyncpg rejects.
_UNSUPPORTED_PARAMS = (
    "sslmode", "channel_binding", "options", "application_name",
    "target_session_attrs", "connect_timeout", "fallback_application_name",
    "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count",
    "tcp_user_timeout", "gssencmode", "krbsrvname", "passfile",
)


def normalize_url(raw_url: str) -> str:
    """Return the URL with the asyncpg driver and without libpq-only parameters.

    Raises InvalidInput for anything that isn't a PostgreSQL URL. Uses string rules
    rather than urlparse, which doesn't reliably handle `postgresql+driver://` schemes.
    """
    url = raw_url.strip()
    for old, new in _SCHEME_REWRITES:
        if url.startswith(old):
            url = new + url[len(old):]
            break
    else:
        raise InvalidInput(errors.UNSUPPORTED_SCHEME)

    for param in _UNSUPPORTED_PARAMS:
        url = re.sub(
            rf"([?&]){re.escape(param)}=[^&]*(&?)",
            lambda m: m.group(1) if m.group(2) else "",
            url,
        )
    return re.sub(r"[?&]$", "", url)


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def ensure_host_allowed(url: str) -> None:
    """Reject hosts on private, loopback, link-local or reserved networks (SSRF guard).

    Every resolved address must be public. Skipped when private hosts are allowed
    (development, or ALLOW_PRIVATE_DB_HOSTS for self-hosting). Unresolvable hosts are
    left to the connection attempt, which reports them as unreachable.
    """
    if settings.private_db_hosts_allowed:
        return
    try:
        parsed = make_url(url)
    except ArgumentError:
        raise InvalidInput(errors.INVALID_CONNECTION_STRING) from None
    if not parsed.host:
        raise InvalidInput(errors.INVALID_CONNECTION_STRING)
    try:
        addresses = await _resolve(parsed.host, parsed.port or 5432)
    except OSError:
        return
    if not all(_is_public(address) for address in addresses):
        logger.info("Blocked connection to non-public host %s", parsed.host)
        raise InvalidInput(errors.HOST_NOT_ALLOWED)


@asynccontextmanager
async def open_target_engine(url: str) -> AsyncIterator[AsyncEngine]:
    """A short-lived engine for one operation on a user's database; always disposed."""
    await ensure_host_allowed(url)
    engine = create_async_engine(
        url, poolclass=NullPool, connect_args={"timeout": CONNECT_TIMEOUT_SECONDS}
    )
    try:
        yield engine
    finally:
        await engine.dispose()


def decrypt_url(encrypted_url: str) -> str:
    """Decrypt a stored connection string and re-normalise it (older rows may predate the rules)."""
    return normalize_url(decrypt(encrypted_url))


async def check_reachable(url: str) -> str | None:
    """Open and close one connection. Returns None on success, else a user-safe message."""
    try:
        async with open_target_engine(url) as engine, engine.connect():
            return None
    except InvalidInput as exc:
        return exc.message
    except Exception as exc:  # driver errors; messages may contain credentials
        logger.info("Connection test failed: %s", errors.exception_summary(exc))
        return errors.describe_connection_error(exc)
