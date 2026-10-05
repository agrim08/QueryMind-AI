"""Embedding a large schema in batches survives the per-minute rate limit (Phase 4.2)."""
import asyncio

import pytest
from google.genai import errors as genai_errors

from app.services import schema_indexer
from app.services.schema_indexer import embed_documents


def _rate_limited() -> genai_errors.ClientError:
    return genai_errors.ClientError(429, {"error": {"code": 429, "message": "quota"}})


def _run(docs: list[str], failures: list[Exception | None], monkeypatch) -> tuple[list[dict], list, list[float]]:
    """Embed `docs`; each embedding call raises the next entry of `failures` (None succeeds)."""
    sleeps: list[float] = []

    async def embed_texts(batch, task_type):
        failure = failures.pop(0) if failures else None
        if failure:
            raise failure
        return [[float(len(text))] for text in batch]

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(schema_indexer, "embed_texts", embed_texts)
    monkeypatch.setattr(schema_indexer.asyncio, "sleep", sleep)
    monkeypatch.setattr(schema_indexer, "EMBED_BATCH_SIZE", 2)
    vectors: list = []

    async def collect() -> list[dict]:
        return [event async for event in embed_documents(docs, vectors)]

    return asyncio.run(collect()), vectors, sleeps


def test_batches_report_progress(monkeypatch):
    events, vectors, sleeps = _run(["a", "bb", "ccc"], [], monkeypatch)
    assert vectors == [[1.0], [2.0], [3.0]]
    assert [e for e in events if e["type"] == "progress"] == [
        {"type": "progress", "current": 2, "total": 3},
        {"type": "progress", "current": 3, "total": 3},
    ]
    assert sleeps == []


def test_a_rate_limited_batch_waits_a_minute_and_is_retried_once(monkeypatch):
    events, vectors, sleeps = _run(["a", "bb", "ccc"], [None, _rate_limited()], monkeypatch)
    assert vectors == [[1.0], [2.0], [3.0]]
    assert sleeps == [schema_indexer.EMBED_RATE_LIMIT_WAIT_S]
    assert any(e["type"] == "status" and "waiting a minute" in e["message"] for e in events)


def test_a_second_refusal_fails_the_run(monkeypatch):
    with pytest.raises(genai_errors.ClientError):
        _run(["a", "bb"], [_rate_limited(), _rate_limited()], monkeypatch)


def test_other_errors_are_not_retried(monkeypatch):
    with pytest.raises(RuntimeError):
        _run(["a"], [RuntimeError("network down")], monkeypatch)
