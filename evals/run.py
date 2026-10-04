"""Run the accuracy eval: python -m evals.run [dataset ...] [options]  (from backend/, after evals.setup)

  (no dataset)     all datasets
  --core           only the core set (15 questions, every question type)
  --ids c01,p05    only these cases
  --budget N       at most N new Gemini calls this run (never more than today's eval budget)
  --check-gold     run only the gold queries (no Gemini calls) and show their row counts
  --reindex        rebuild the schema indexes first (one embedding call per 100 tables)
  --interval S     seconds between Gemini calls (keeps inside the free tier's per-minute limit)

Each question goes through the real query pipeline (retrieve -> generate -> validate ->
execute), exactly as in the app, and its rows are compared with the gold query's rows.

Gemini answers are cached by exact request, so re-running only costs quota for questions whose
prompt changed. Questions that don't fit today's budget are reported as pending and picked up
by the next run. Reports go to evals/reports/ (latest.md is the readable summary).
"""
import os

from evals.config import APP_DATABASE, database_url

# Point the app at the local eval database before any app module reads its settings, so
# an eval run can never touch production data. Development mode allows localhost targets.
os.environ["DATABASE_URL"] = database_url(APP_DATABASE)
os.environ["ENVIRONMENT"] = "development"

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from dataclasses import asdict, dataclass  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.core import errors  # noqa: E402
from app.core.ai_config import GENERATION_MODEL  # noqa: E402
from app.core.security import encrypt  # noqa: E402
from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.models.models import DBConnection, User  # noqa: E402
from app.services import query_pipeline, sql_generator  # noqa: E402
from app.services.query_executor import QueryResult, execute_query  # noqa: E402
from app.services.query_pipeline import PipelineOutcome, run_pipeline  # noqa: E402
from app.services.schema_indexer import index_connection  # noqa: E402
from app.services.schema_retriever import retrieve_schema  # noqa: E402
from evals.compare import compare_results  # noqa: E402
from evals.config import DEFAULT_INTERVAL_S, REPORTS_DIR, SOURCES  # noqa: E402
from evals.dataset import Case, load_cases  # noqa: E402
from evals.generation_cache import GenerationCache, request_key  # noqa: E402
from evals.quota import QuotaLedger  # noqa: E402

logger = logging.getLogger("evals.run")

EVAL_CLERK_ID = "eval-runner"
PASSING = {"exact", "extra_columns", "declined", "asked"}
# Consecutive refused generations that mean Gemini is out of quota for now.
MAX_REFUSALS = 2


@dataclass
class CaseResult:
    dataset: str
    id: str
    tags: list[str]
    question: str
    # exact | extra_columns | declined | asked (passing; "asked" only on ambiguous cases)
    # mismatch | invalid_sql | error | wrongly_declined | should_decline | asked_unnecessarily |
    # answered_in_words (failing)
    verdict: str
    generated_sql: str
    error: str | None
    rows: int | None
    cached: bool  # answer reused from an earlier run; timings then exclude Gemini
    attempts: int  # 2 when the pipeline retried after a fixable error
    first_sql_ms: int | None
    total_ms: int

    @property
    def passed(self) -> bool:
        return self.verdict in PASSING


@dataclass(frozen=True)
class Target:
    """A dataset with its indexed connection, ready to be asked questions."""

    dataset: str
    connection: DBConnection
    cases: list[Case]


async def ensure_connection(dataset: str) -> DBConnection:
    """The eval user's connection to the dataset, created on first run."""
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.clerk_id == EVAL_CLERK_ID))
        if user is None:
            user = User(clerk_id=EVAL_CLERK_ID, email="evals@localhost", full_name="Eval runner")
            session.add(user)
            await session.flush()
        connection = await session.scalar(
            select(DBConnection).where(DBConnection.user_id == user.id, DBConnection.name == dataset)
        )
        if connection is None:
            connection = DBConnection(
                user_id=user.id,
                name=dataset,
                encrypted_conn_string=encrypt(database_url(SOURCES[dataset].database)),
            )
            session.add(connection)
        await session.commit()
        await session.refresh(connection)
        return connection


async def ensure_indexed(connection: DBConnection, force: bool) -> None:
    if connection.indexed_at is not None and not force:
        return
    async for event in index_connection(connection.id, connection.user_id, connection.encrypted_conn_string):
        if event["type"] == "error":
            raise RuntimeError(f"Indexing {connection.name} failed: {event['message']}")
        if event["type"] == "done":
            logger.info("Indexed %s: %d tables", connection.name, event["table_count"])


async def gold_results(connection: DBConnection, case: Case) -> list[QueryResult]:
    results = [await execute_query(connection.encrypted_conn_string, sql) for sql in case.gold]
    for sql, result in zip(case.gold, results):
        if result.truncated:
            raise ValueError(f"{case.id}: gold query returns more than the row cap; narrow it: {sql}")
    return results


async def run_case(target: Target, case: Case, cached: bool) -> CaseResult:
    connection = target.connection
    outcome = PipelineOutcome()
    started = time.perf_counter()
    first_sql_ms: int | None = None
    async with AsyncSessionLocal() as session:
        async for event in run_pipeline(
            session, connection.id, connection.encrypted_conn_string, case.question, outcome
        ):
            if event["type"] == "sql_chunk" and first_sql_ms is None:
                first_sql_ms = int((time.perf_counter() - started) * 1000)
    total_ms = int((time.perf_counter() - started) * 1000)

    sql = outcome.generated_sql
    declined = outcome.kind in ("declined", "not_allowed")
    if case.cannot_answer:
        verdict = "declined" if declined else "should_decline"
    elif declined:
        verdict = "wrongly_declined"
    elif outcome.kind == "clarify":
        # Asking is right only when the question really has several readings.
        verdict = "asked" if "ambiguous" in case.tags else "asked_unnecessarily"
    elif outcome.kind == "message":
        verdict = "answered_in_words"  # these cases need rows
    elif outcome.status == "validation_error":
        verdict = "invalid_sql"
    elif outcome.status != "success" or outcome.result is None:
        verdict = "error"
    else:
        verdicts = [compare_results(g.rows, outcome.result.rows) for g in await gold_results(connection, case)]
        verdict = next((v for v in verdicts if v != "mismatch"), "mismatch")

    return CaseResult(
        dataset=target.dataset,
        id=case.id,
        tags=list(case.tags),
        question=case.question,
        verdict=verdict,
        generated_sql=sql,
        error=outcome.error_message,
        rows=outcome.result.row_count if outcome.result else None,
        cached=cached,
        attempts=outcome.attempts,
        first_sql_ms=None if cached else first_sql_ms,
        total_ms=total_ms,
    )


def summarize(results: list[CaseResult], pending: list[str]) -> dict:
    by_tag: dict[str, list[int]] = {}
    for r in results:
        for tag in r.tags:
            counts = by_tag.setdefault(tag, [0, 0])
            counts[0] += r.passed
            counts[1] += 1
    live = [r for r in results if not r.cached]
    first_sql = [r.first_sql_ms for r in live if r.first_sql_ms is not None]
    passed = sum(r.passed for r in results)
    return {
        "passed": passed,
        "scored": len(results),
        "pending": pending,
        "accuracy": round(passed / len(results), 3) if results else None,
        "by_tag": {tag: {"passed": p, "total": t} for tag, (p, t) in sorted(by_tag.items())},
        "retried": sum(r.attempts > 1 for r in results),
        "passed_after_retry": sum(r.attempts > 1 and r.passed for r in results),
        "median_first_sql_ms": int(statistics.median(first_sql)) if first_sql else None,
        "median_total_ms": int(statistics.median(r.total_ms for r in live)) if live else None,
    }


def render_markdown(stamp: str, results: list[CaseResult], summaries: dict[str, dict]) -> str:
    lines = [f"# Eval report {stamp}", "", f"Model: `{GENERATION_MODEL}`", ""]
    for dataset, s in summaries.items():
        score = f"{s['passed']}/{s['scored']} ({s['accuracy']:.0%})" if s["scored"] else "nothing scored"
        lines += [f"## {dataset}: {score}", ""]
        if s["pending"]:
            lines += [f"Pending (next run): {', '.join(s['pending'])}", ""]
        if s["retried"]:
            lines += [f"Retried after a fixable error: {s['retried']} ({s['passed_after_retry']} then passed)", ""]
        if s["median_first_sql_ms"] is not None:
            lines += [
                f"Median first SQL token {s['median_first_sql_ms']} ms, "
                f"median answer {s['median_total_ms']} ms (new Gemini calls only)",
                "",
            ]
        if s["by_tag"]:
            lines += ["| Tag | Passed |", "|---|---|"]
            lines += [f"| {tag} | {c['passed']}/{c['total']} |" for tag, c in s["by_tag"].items()]
        failures = [r for r in results if r.dataset == dataset and not r.passed]
        if failures:
            lines += ["", "Failures:", ""]
        for r in failures:
            lines.append(f"- **{r.id}** `{r.verdict}`: {r.question}")
            lines.append(f"  - SQL: `{' '.join(r.generated_sql.split())[:400]}`")
            if r.error:
                lines.append(f"  - Message: {r.error}")
        lines.append("")
    return "\n".join(lines)


def write_report(results: list[CaseResult], summaries: dict[str, dict]) -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = {
        "model": GENERATION_MODEL,
        "created_at": stamp,
        "summaries": summaries,
        "cases": [{**asdict(r), "passed": r.passed} for r in results],
    }
    (REPORTS_DIR / f"{stamp}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = render_markdown(stamp, results, summaries)
    (REPORTS_DIR / "latest.md").write_text(markdown, encoding="utf-8")
    logger.info("\n%s", markdown)
    logger.info("Report: %s", REPORTS_DIR / "latest.md")


async def check_gold(targets: list[Target]) -> None:
    """Run every gold query and show row counts, so broken or oversized gold SQL is caught for free."""
    for target in targets:
        for case in target.cases:
            if case.cannot_answer:
                logger.info("  %s  (should decline)", case.id)
                continue
            try:
                counts = [r.row_count for r in await gold_results(target.connection, case)]
                logger.info("  %s  rows=%s", case.id, counts)
            except Exception as exc:  # report every broken case, not just the first
                logger.error("  %s  FAILED: %s", case.id, errors.exception_summary(exc))


async def _prepare(datasets: list[str], ids: set[str] | None, core_only: bool, reindex: bool) -> list[Target]:
    targets = []
    for dataset in datasets:
        cases = [
            c for c in load_cases(dataset) if (ids is None or c.id in ids) and (c.core or not core_only)
        ]
        if not cases:
            continue
        connection = await ensure_connection(dataset)
        await ensure_indexed(connection, reindex)
        targets.append(Target(dataset, connection, cases))
    return targets


async def run_evals(
    datasets: list[str],
    *,
    budget: int | None = None,
    ids: set[str] | None = None,
    core_only: bool = False,
    reindex: bool = False,
    interval: float = DEFAULT_INTERVAL_S,
    gold_only: bool = False,
) -> None:
    """Score the pipeline on the given datasets, spending at most today's eval budget."""
    try:
        targets = await _prepare(datasets, ids, core_only, reindex)
        if gold_only:
            await check_gold(targets)
            return

        ledger = QuotaLedger()
        budget = ledger.remaining() if budget is None else min(budget, ledger.remaining())
        cache = GenerationCache()
        live_stream_sql = query_pipeline.stream_sql
        query_pipeline.stream_sql = cache.wrap(sql_generator.stream_sql, on_live_call=ledger.record_call)
        try:
            results, pending = await _score(targets, cache, budget, interval)
        finally:
            query_pipeline.stream_sql = live_stream_sql

        summaries = {
            t.dataset: summarize(
                [r for r in results if r.dataset == t.dataset],
                [case_id for dataset, case_id in pending if dataset == t.dataset],
            )
            for t in targets
        }
        write_report(results, summaries)
    finally:
        await engine.dispose()


async def _score(
    targets: list[Target],
    cache: GenerationCache,
    budget: int,
    interval: float,
) -> tuple[list[CaseResult], list[tuple[str, str]]]:
    """Ask every question that is cached or fits the budget; core questions first."""
    work = sorted(((t, c) for t in targets for c in t.cases), key=lambda tc: not tc[1].core)
    results: list[CaseResult] = []
    pending: list[tuple[str, str]] = []
    refusals = 0
    logger.info("%d questions, budget %d new Gemini calls", len(work), budget)

    for target, case in work:
        async with AsyncSessionLocal() as session:
            docs = await retrieve_schema(session, target.connection.id, case.question)
        cached = request_key(case.question, docs) in cache
        # Every Gemini call counts, retries included (cache.live_calls).
        if not cached and (cache.live_calls >= budget or refusals >= MAX_REFUSALS):
            pending.append((target.dataset, case.id))
            continue
        if not cached and cache.live_calls:
            await asyncio.sleep(interval)

        result = await run_case(target, case, cached)
        if not cached and result.error == errors.GENERATION_FAILED:
            # A refusal (usually quota) says nothing about accuracy: retry it next run.
            refusals += 1
            pending.append((target.dataset, case.id))
            if refusals == MAX_REFUSALS:
                logger.warning("Gemini refused %d requests in a row (quota). No more new calls this run.", refusals)
            continue
        refusals = 0
        results.append(result)
        source = "cached" if cached else f"{result.total_ms} ms"
        if result.attempts > 1:
            source += ", retried"
        logger.info("%s %s [%s] %s", case.id, "PASS" if result.passed else "FAIL", result.verdict, source)
    return results, pending


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QueryMind accuracy eval")
    parser.add_argument("datasets", nargs="*", choices=sorted(SOURCES), metavar="dataset")
    parser.add_argument("--core", action="store_true")
    parser.add_argument("--ids")
    parser.add_argument("--budget", type=int)
    parser.add_argument("--check-gold", action="store_true")
    parser.add_argument("--reindex", action="store_true")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    args = parse_args()
    asyncio.run(
        run_evals(
            args.datasets or sorted(SOURCES),
            budget=args.budget,
            ids=set(args.ids.split(",")) if args.ids else None,
            core_only=args.core,
            reindex=args.reindex,
            interval=args.interval,
            gold_only=args.check_gold,
        )
    )
