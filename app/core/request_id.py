"""Request IDs: one id per request, on every log line and in the `X-Request-ID` response header.

A caller's `X-Request-ID` (from a proxy or a client retry) is kept only if it looks like an id,
so nobody can write arbitrary text into our logs. A plain ASGI middleware, not BaseHTTPMiddleware,
so streamed (SSE) responses pass through untouched. Background tasks started during a request
inherit its id, because asyncio copies the context into new tasks.
"""
import contextvars
import logging
import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "x-request-id"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    """The id of the request being handled, or "-" outside a request."""
    return _request_id.get()


def choose_request_id(incoming: str | None) -> str:
    """The caller's id if it is safe to log, otherwise a new one."""
    if incoming and _SAFE_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex[:16]


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers", [])).get(HEADER.encode(), b"").decode("latin-1")
        request_id = choose_request_id(incoming)
        token = _request_id.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (HEADER.encode(), request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Adds `request_id` to every log record, so the format can include it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Root logging with the request id on each line: `… INFO [3f2a…] app.services.x: message`."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestIdFilter())
