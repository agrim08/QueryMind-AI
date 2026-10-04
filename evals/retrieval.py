"""Retrieval eval: does search find every table a question needs?  python -m evals.retrieval

The eval databases are small enough that the app sends their whole schema, so this forces
the large-schema search path instead and checks, for every question with gold SQL, whether
all the tables the gold query reads are among the tables the model would be shown.

Compares three strategies, top RETRIEVAL_TOP_K_TABLES each:
  vector         nearest tables by embedding only
  vector+links   ... plus the tables they reference by foreign key (the pre-1.4 behaviour)
  hybrid+links   vector + full text + table-name similarity (RRF), plus foreign-key links

Costs one embedding call per question (shared by the strategies) and no generation calls.
Writes evals/reports/retrieval-latest.md.
"""
# `evals.run` must be imported before anything else from the app: it points the app at the
# local eval database.
from evals import run  # isort: skip

import asyncio
import logging

from app.core.ai_config import RETRIEVAL_TOP_K_TABLES
from app.db.session import AsyncSessionLocal, engine
from app.services.embeddings import embed_query
from app.services.schema_retriever import with_linked_tables
from app.services.schema_store import TableDoc, search_tables, search_tables_hybrid
from evals.config import REPORTS_DIR, SOURCES
from evals.dataset import load_cases, needed_tables

logger = logging.getLogger("evals.retrieval")

STRATEGIES = ("vector", "vector+links", "hybrid+links")
# Spaces embedding calls well inside the free tier's per-minute limit.
EMBED_INTERVAL_S = 1.0


async def _retrieve(connection_id, question: str, vector: list[float]) -> dict[str, list[TableDoc]]:
    async with AsyncSessionLocal() as session:
        nearest = await search_tables(session, connection_id, vector, RETRIEVAL_TOP_K_TABLES)
        hybrid = await search_tables_hybrid(session, connection_id, question, vector, RETRIEVAL_TOP_K_TABLES)
        return {
            "vector": nearest,
            "vector+links": await with_linked_tables(session, connection_id, nearest),
            "hybrid+links": await with_linked_tables(session, connection_id, hybrid),
        }


async def evaluate(dataset: str) -> dict[str, dict]:
    connection = await run.ensure_connection(dataset)
    await run.ensure_indexed(connection, force=False)
    scores = {s: {"found": 0, "total": 0, "tables_shown": 0, "missed": []} for s in STRATEGIES}
    for case in load_cases(dataset):
        if case.cannot_answer:
            continue
        needed = needed_tables(case.gold[0])
        vector = await embed_query(case.question)
        for strategy, docs in (await _retrieve(connection.id, case.question, vector)).items():
            shown = {d.table_name.lower() for d in docs}
            score = scores[strategy]
            score["total"] += 1
            score["tables_shown"] += len(shown)
            if needed <= shown:
                score["found"] += 1
            else:
                score["missed"].append(f"{case.id} (missing {', '.join(sorted(needed - shown))})")
        await asyncio.sleep(EMBED_INTERVAL_S)
    return scores


def render(results: dict[str, dict[str, dict]]) -> str:
    lines = [
        "# Retrieval eval",
        "",
        f"Share of questions where every table the gold SQL reads is shown to the model (top {RETRIEVAL_TOP_K_TABLES}).",
        "",
        "| Dataset | Strategy | All tables found | Avg tables shown |",
        "|---|---|---|---|",
    ]
    for dataset, scores in results.items():
        for strategy, s in scores.items():
            share = s["found"] / s["total"] if s["total"] else 0
            lines.append(
                f"| {dataset} | {strategy} | {s['found']}/{s['total']} ({share:.0%}) "
                f"| {s['tables_shown'] / max(s['total'], 1):.1f} |"
            )
    for dataset, scores in results.items():
        for strategy, s in scores.items():
            if s["missed"]:
                lines += ["", f"**{dataset} / {strategy} missed:** {'; '.join(s['missed'])}"]
    return "\n".join(lines) + "\n"


async def main() -> None:
    try:
        results = {dataset: await evaluate(dataset) for dataset in sorted(SOURCES)}
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
    asyncio.run(main())
