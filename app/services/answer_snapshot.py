"""Answer snapshots: what the user saw for a question, kept on its QueryLog row for History.

Result rows are the customer's own data, so a snapshot is bounded: the presentation (headline,
chart) plus at most MAX_ROWS rows, at most MAX_BYTES in all. When an answer is larger, rows are
dropped first, then the chart; the headline always stays. Snapshots are read only by their owner
and are deleted with the question, its connection or the account (security.md §6).
"""
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.exceptions import NotFound
from app.models.models import QueryLog
from app.services.query_executor import QueryResult

MAX_ROWS = 50
MAX_BYTES = 32_000


def _encode(snapshot: dict) -> str:
    # Same conversion as the SSE stream (dates, decimals → text), so History shows what was seen.
    return json.dumps(snapshot, default=str, ensure_ascii=False)


def build(answer: dict, result: QueryResult) -> dict:
    """The snapshot of one answered question, within MAX_ROWS rows and MAX_BYTES."""
    snapshot = {
        "answer": answer,
        "columns": result.columns,
        "rows": result.rows[:MAX_ROWS],
        "row_count": result.row_count,
        "truncated": result.truncated,
    }
    encoded = _encode(snapshot)
    while len(encoded.encode()) > MAX_BYTES and snapshot["rows"]:
        snapshot["rows"] = snapshot["rows"][: len(snapshot["rows"]) // 2]
        encoded = _encode(snapshot)
    if len(encoded.encode()) > MAX_BYTES:
        snapshot["answer"] = {**answer, "chart": {"kind": "table"}}
        encoded = _encode(snapshot)
    return json.loads(encoded)


async def get(session: AsyncSession, user_id: uuid.UUID, log_id: uuid.UUID) -> dict:
    """The saved answer to one of this user's questions; NotFound if it has none."""
    snapshot = await session.scalar(
        select(QueryLog.answer_snapshot).where(QueryLog.id == log_id, QueryLog.user_id == user_id)
    )
    if snapshot is None:
        raise NotFound(errors.ANSWER_NOT_SAVED)
    return snapshot
