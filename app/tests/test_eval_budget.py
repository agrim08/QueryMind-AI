"""Unit tests for the eval budget helpers (evals/quota.py, evals/generation_cache.py)."""
import asyncio
from datetime import date, datetime, timezone

import pytest

from app.services.schema_store import TableDoc
from evals import generation_cache
from evals.generation_cache import GenerationCache, request_key
from evals.quota import quota_day

DOCS = [TableDoc("artist", "Table: artist\nColumns:\n- name (VARCHAR)", 0.0)]


class TestQuotaDay:
    def test_summer_resets_at_midnight_pdt(self):
        # 2026-10-04 is daylight time: midnight PDT is 07:00 UTC.
        assert quota_day(datetime(2026, 10, 4, 6, 59, tzinfo=timezone.utc)) == date(2026, 10, 3)
        assert quota_day(datetime(2026, 10, 4, 7, 0, tzinfo=timezone.utc)) == date(2026, 10, 4)

    def test_winter_resets_at_midnight_pst(self):
        assert quota_day(datetime(2026, 12, 1, 7, 59, tzinfo=timezone.utc)) == date(2026, 11, 30)
        assert quota_day(datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)) == date(2026, 12, 1)

    def test_daylight_saving_boundaries_2026(self):
        # Starts Sunday 8 March 2026 at 10:00 UTC, ends Sunday 1 November 2026 at 09:00 UTC.
        assert quota_day(datetime(2026, 3, 8, 9, 59, tzinfo=timezone.utc)) == date(2026, 3, 8)  # 01:59 PST
        assert quota_day(datetime(2026, 11, 1, 8, 59, tzinfo=timezone.utc)) == date(2026, 11, 1)  # 01:59 PDT


class TestRequestKey:
    def test_same_request_same_key(self):
        assert request_key("top artists?", DOCS) == request_key("top artists?", DOCS)

    def test_question_or_tables_change_the_key(self):
        other_docs = [TableDoc("album", "Table: album", 0.0)]
        assert request_key("top artists?", DOCS) != request_key("top albums?", DOCS)
        assert request_key("top artists?", DOCS) != request_key("top artists?", other_docs)


@pytest.fixture
def cache(tmp_path, monkeypatch) -> GenerationCache:
    monkeypatch.setattr(generation_cache, "GENERATION_CACHE_FILE", tmp_path / "generations.json")
    return GenerationCache()


def _collect(stream) -> str:
    async def run() -> str:
        return "".join([chunk async for chunk in stream])

    return asyncio.run(run())


class TestGenerationCache:
    def test_miss_calls_gemini_and_saves_then_hit_reuses(self, cache):
        calls: list[str] = []

        async def live(question, docs, *options):
            calls.append(question)
            yield "SELECT name "
            yield "FROM artist"

        stream_sql = cache.wrap(live, on_live_call=lambda: None)
        assert _collect(stream_sql("q", DOCS)) == "SELECT name FROM artist"
        assert _collect(stream_sql("q", DOCS)) == "SELECT name FROM artist"
        assert calls == ["q"]
        assert request_key("q", DOCS) in GenerationCache()  # persisted for the next run

    def test_failed_stream_is_not_saved(self, cache):
        async def refused(question, docs, *options):
            yield "SELECT"
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

        with pytest.raises(RuntimeError):
            _collect(cache.wrap(refused, on_live_call=lambda: None)("q", DOCS))
        assert request_key("q", DOCS) not in cache

    def test_live_calls_are_reported(self, cache):
        recorded: list[int] = []

        async def live(question, docs, *options):
            yield "SELECT 1"

        stream_sql = cache.wrap(live, on_live_call=lambda: recorded.append(1))
        _collect(stream_sql("q", DOCS))
        _collect(stream_sql("q", DOCS))
        assert recorded == [1]
