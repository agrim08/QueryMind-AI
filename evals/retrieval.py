"""Retrieval eval: does search find every table a question needs?

    python -m evals.retrieval [--reindex]     (--reindex: rebuild the indexes first)

For every question with gold SQL, checks whether all the tables the gold query reads are among
the tables the model would be shown, and how much schema text that costs.

  chinook, pagila        small enough to be sent whole in the app; the search path is forced
  chinook_xl, pagila_xl  the same questions among ~180 look-alike tables (Phase 4.2), large
                         enough that the app searches

Strategies:
  hybrid+links  the fixed Phase 1.4 baseline: top RETRIEVAL_TOP_K_TABLES by vector + full text +
                table-name similarity (RRF), plus the tables they reference by foreign key
  app           what the app's search path does now (schema_retriever.search_schema)

Question embeddings are cached in evals/.cache/embeddings.json, so re-running after a retriever
change costs no API calls. Writes evals/reports/retrieval-latest.md.
"""
# `evals.run` must be imported before anything else from the app: it points the app at the
# local eval database.
from evals import run  # isort: skip

import asyncio
import json
import logging
import sys

from app.core import errors
from app.core.ai_config import EMBED_RATE_LIMIT_WAIT_S, EMBEDDING_MODEL
from app.db.session import AsyncSessionLocal, engine
from app.services.embeddings import embed_query
from app.services.schema_retriever import search_schema, with_linked_tables
from app.services.schema_store import TableDoc, schema_size, search_tables_hybrid
from evals.config import CACHE_DIR, REPORTS_DIR, SOURCES
from evals.dataset import load_cases, needed_tables

logger = logging.getLogger("evals.retrieval")

# Variants compared with the app: (top-k tables, foreign-key hops, bridge tables).
VARIANTS: dict[str, tuple[int, int, bool]] = {
    "hybrid+links": (6, 1, False),  # the Phase 1.4 baseline
    "k12+links": (12, 1, False),
    "k6+bridges+2hops": (6, 2, True),
    "k12+bridges+2hops": (12, 2, True),
}
STRATEGIES = (*VARIANTS, "app")
EMBEDDING_CACHE_FILE = CACHE_DIR / "embeddings.json"
# Spaces embedding calls well inside the free tier's per-minute limit.
EMBED_INTERVAL_S = 1.0
RATE_LIMIT_RETRIES = 3


class EmbeddingCache:
    """Question embeddings by model and text; only new questions cost an API call."""

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = (
            json.loads(EMBEDDING_CACHE_FILE.read_text(encoding="utf-8")) if EMBEDDING_CACHE_FILE.exists() else {}
        )

    async def embed(self, question: str) -> list[float]:
        key = f"{EMBEDDING_MODEL}\n{question}"
        if key not in self._vectors:
            self._vectors[key] = await self._embed_with_waits(question)
            EMBEDDING_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            EMBEDDING_CACHE_FILE.write_text(json.dumps(self._vectors), encoding="utf-8")
            await asyncio.sleep(EMBED_INTERVAL_S)
        return self._vectors[key]

    @staticmethod
    async def _embed_with_waits(question: str) -> list[float]:
        # Free tier: 100 embedded texts a minute, and indexing a large schema can use a whole
        # minute's allowance, so a refused call waits for the next window (up to three times).
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                return await embed_query(question)
            except Exception as exc:
                if attempt == RATE_LIMIT_RETRIES or not errors.is_ai_rate_limited(exc):
                    raise
                logger.info("Embedding rate limit; waiting a minute")
                await asyncio.sleep(EMBED_RATE_LIMIT_WAIT_S)
        raise AssertionError("unreachable")


async def _retrieve(connection_id, question: str, vector: list[float]) -> dict[str, list[TableDoc]]:
    async with AsyncSessionLocal() as session:
        results: dict[str, list[TableDoc]] = {}
        for name, (top_k, hops, bridges) in VARIANTS.items():
            ranked = await search_tables_hybrid(session, connection_id, question, vector, top_k)
            results[name] = await with_linked_tables(session, connection_id, ranked, hops, bridges)
        results["app"] = await search_schema(session, connection_id, question, vector)
        return results


async def evaluate(dataset: str, embeddings: EmbeddingCache, reindex: bool) -> dict[str, dict]:
    connection = await run.ensure_connection(dataset)
    await run.ensure_indexed(connection, force=reindex)
    async with AsyncSessionLocal() as session:
        size = await schema_size(session, connection.id)
    logger.info("%s: %d characters of table documents", dataset, size)
    scores = {s: {"found": 0, "total": 0, "tables_shown": 0, "chars_shown": 0, "missed": []} for s in STRATEGIES}
    for case in load_cases(dataset):
        if case.cannot_answer:
            continue
        needed = needed_tables(case.gold[0])
        vector = await embeddings.embed(case.question)
        for strategy, docs in (await _retrieve(connection.id, case.question, vector)).items():
            shown = {d.table_name.lower() for d in docs}
            score = scores[strategy]
            score["total"] += 1
            score["tables_shown"] += len(shown)
            score["chars_shown"] += sum(len(d.doc) for d in docs)
            if needed <= shown:
                score["found"] += 1
            else:
                score["missed"].append(f"{case.id} (missing {', '.join(sorted(needed - shown))})")
    return scores


def render(results: dict[str, dict[str, dict]]) -> str:
    lines = [
        "# Retrieval eval",
        "",
        "Share of questions where every table the gold SQL reads is shown to the model, and what it costs.",
        "",
        "| Dataset | Strategy | All tables found | Avg tables shown | Avg schema chars |",
        "|---|---|---|---|---|",
    ]
    for dataset, scores in results.items():
        for strategy, s in scores.items():
            total = max(s["total"], 1)
            lines.append(
                f"| {dataset} | {strategy} | {s['found']}/{s['total']} ({s['found'] / total:.0%}) "
                f"| {s['tables_shown'] / total:.1f} | {s['chars_shown'] / total:,.0f} |"
            )
    for dataset, scores in results.items():
        for strategy, s in scores.items():
            if s["missed"]:
                lines += ["", f"**{dataset} / {strategy} missed:** {'; '.join(s['missed'])}"]
    return "\n".join(lines) + "\n"


async def main(reindex: bool) -> None:
    embeddings = EmbeddingCache()
    try:
        results = {dataset: await evaluate(dataset, embeddings, reindex) for dataset in sorted(SOURCES)}
    finally:
        await engine.dispose()
    report = render(results)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "retrieval-latest.md").write_text(report, encoding="utf-8")
    logger.info("\n%s", report)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    asyncio.run(main(reindex="--reindex" in sys.argv[1:]))
