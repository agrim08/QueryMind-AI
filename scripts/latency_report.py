"""Latency report: p50 / p95 per step of answering, from the app database (read-only).

    python -m scripts.latency_report [days]     (from backend/; default: the last 7 days)

Every question stores millisecond milestones in `query_logs.timings` (query_pipeline):
setup (sign-in, ownership, meter, context) before the pipeline, then retrieved, first_sql,
generated, validated, executed and total since the pipeline started. Targets (ROADMAP 4.3):
first SQL token under 2 s and the full answer under 8 s at p95, both from the request.
"""
import asyncio
import sys

from sqlalchemy import text

from app.db.session import engine

# (label, milliseconds expression over `t` = timings JSONB). Steps are the gaps between milestones.
STEPS: list[tuple[str, str]] = [
    ("setup (before the pipeline)", "(t->>'setup')::int"),
    ("retrieve schema", "(t->>'retrieved')::int"),
    ("generate (Gemini or reuse)", "(t->>'generated')::int - (t->>'retrieved')::int"),
    ("validate", "(t->>'validated')::int - (t->>'generated')::int"),
    ("execute (your database)", "(t->>'executed')::int - (t->>'validated')::int"),
    ("first SQL token, from request", "(t->>'setup')::int + (t->>'first_sql')::int"),
    ("full answer, from request", "(t->>'setup')::int + (t->>'total')::int"),
]


async def report(days: int) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        print(f"Questions with timings, last {days} days\n")
        print(f"{'step':34} {'n':>5} {'p50 ms':>8} {'p95 ms':>8}")
        for label, expr in STEPS:
            row = (
                await conn.execute(
                    text(
                        f"SELECT count(v), percentile_cont(0.5) WITHIN GROUP (ORDER BY v), "
                        f"percentile_cont(0.95) WITHIN GROUP (ORDER BY v) "
                        f"FROM (SELECT {expr} AS v FROM query_logs, LATERAL (SELECT timings AS t) x "
                        f"WHERE timings IS NOT NULL AND created_at > now() - make_interval(days => :days)) s "
                        f"WHERE v IS NOT NULL"
                    ),
                    {"days": days},
                )
            ).one()
            n, p50, p95 = row
            print(f"{label:34} {n:>5} {p50 or 0:>8.0f} {p95 or 0:>8.0f}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(report(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
