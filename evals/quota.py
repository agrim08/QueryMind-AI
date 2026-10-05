"""Daily Gemini budget for evals.

The free tier's daily request limit resets at midnight US Pacific time and is shared with the
app. The ledger counts the generation calls evals make per quota day, so manual and scheduled
runs together never take more than DAILY_EVAL_BUDGET. It can't see the app's own calls.
"""
import json
from datetime import date, datetime, time, timedelta, timezone

from app.core import errors
from evals.config import DAILY_EVAL_BUDGET, QUOTA_LEDGER_FILE

# Days of ledger history to keep.
_KEEP_DAYS = 14


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)


def quota_day(now: datetime) -> date:
    """The Pacific-time date `now` falls on (US daylight saving: 2nd Sunday of March to 1st of November).

    Computed by hand because Windows Pythons often lack time-zone data.
    """
    utc = now.astimezone(timezone.utc)
    dst_start = datetime.combine(_nth_sunday(utc.year, 3, 2), time(10), timezone.utc)  # 2:00 PST
    dst_end = datetime.combine(_nth_sunday(utc.year, 11, 1), time(9), timezone.utc)  # 2:00 PDT
    offset = timedelta(hours=-7 if dst_start <= utc < dst_end else -8)
    return (utc + offset).date()


class QuotaLedger:
    """Generation calls made by evals, per quota day, persisted between runs."""

    def __init__(self) -> None:
        self._calls: dict[str, int] = (
            json.loads(QUOTA_LEDGER_FILE.read_text(encoding="utf-8")) if QUOTA_LEDGER_FILE.exists() else {}
        )

    def _today(self) -> str:
        return quota_day(datetime.now(timezone.utc)).isoformat()

    def remaining(self) -> int:
        return max(0, DAILY_EVAL_BUDGET - self._calls.get(self._today(), 0))

    def record_call(self) -> None:
        today = self._today()
        self._calls[today] = self._calls.get(today, 0) + 1
        cutoff = (date.fromisoformat(today) - timedelta(days=_KEEP_DAYS)).isoformat()
        self._calls = {day: n for day, n in self._calls.items() if day >= cutoff}
        QUOTA_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUOTA_LEDGER_FILE.write_text(json.dumps(self._calls, indent=2), encoding="utf-8")


def is_refusal(error: str | None) -> bool:
    """Gemini didn't answer (quota or rate limit): that says nothing about accuracy, so the
    question is retried next run. A 429 reaches the user as AI_BUSY; other generation failures
    as GENERATION_FAILED."""
    return error in (errors.AI_BUSY, errors.GENERATION_FAILED)
