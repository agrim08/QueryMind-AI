"""Server-Sent Events helpers shared by every streaming endpoint."""
import json
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse(event: dict) -> str:
    """Format one event. `default=str` covers datetime, date, Decimal and UUID values."""
    return f"data: {json.dumps(event, default=str)}\n\n"


def sse_response(events: AsyncIterator[dict]) -> StreamingResponse:
    """Stream an async iterator of event dicts as text/event-stream."""

    async def body() -> AsyncIterator[str]:
        async for event in events:
            yield sse(event)

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)
