"""Saved Gemini answers, keyed by the exact request, so an unchanged question is never paid for twice.

The key covers the model, the full prompt (question, table documents, and any retry feedback,
clarification or business context) and the generation config. Changing the prompt, the
retrieved tables or the model makes a new key, so only those changes cost quota; validator,
executor and scoring changes re-score for free.
"""
import hashlib
import json
from collections.abc import AsyncIterator, Callable

from app.core.ai_config import GENERATION_MODEL
from app.services.schema_store import TableDoc
from app.services.sql_generator import build_request
from evals.config import GENERATION_CACHE_FILE

StreamSql = Callable[..., AsyncIterator[str]]


def request_key(question: str, table_docs: list[TableDoc], *options: object) -> str:
    """Hash of the exact request; `options` are stream_sql's optional inputs, in order."""
    contents, config = build_request(question, table_docs, *options)
    payload = [GENERATION_MODEL, contents, config.model_dump(mode="json", exclude_none=True)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class GenerationCache:
    def __init__(self) -> None:
        self.live_calls = 0  # Gemini calls made through this cache (first answers and retries)
        self._answers: dict[str, str] = (
            json.loads(GENERATION_CACHE_FILE.read_text(encoding="utf-8")) if GENERATION_CACHE_FILE.exists() else {}
        )

    def __contains__(self, key: str) -> bool:
        return key in self._answers

    def _save(self, key: str, sql: str) -> None:
        self._answers[key] = sql
        GENERATION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        GENERATION_CACHE_FILE.write_text(json.dumps(self._answers, indent=1), encoding="utf-8")

    def wrap(self, stream_sql: StreamSql, on_live_call: Callable[[], None]) -> StreamSql:
        """A drop-in `stream_sql` that answers from the cache and saves complete live answers."""

        async def cached_stream_sql(question: str, table_docs: list[TableDoc], *options: object) -> AsyncIterator[str]:
            key = request_key(question, table_docs, *options)
            if key in self._answers:
                yield self._answers[key]
                return
            self.live_calls += 1
            on_live_call()
            chunks: list[str] = []
            async for chunk in stream_sql(question, table_docs, *options):
                chunks.append(chunk)
                yield chunk
            # Only a stream that finished is saved; a failed one raises before this line.
            if sql := "".join(chunks).strip():
                self._save(key, sql)

        return cached_stream_sql
