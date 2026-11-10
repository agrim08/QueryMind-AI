"""Check the app database connection and list its tables (read-only).

    python -m scripts.check_db        (from backend/)

Replaces the old ad-hoc check_tables.py / test_db.py. It never prints the connection URL, and
connection errors are shown with credentials redacted.
"""
import asyncio

from sqlalchemy import text

from app.core.errors import exception_summary
from app.db.session import engine


async def check() -> None:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            rows = await conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1")
            )
            tables = [r[0] for r in rows]
        print(f"Connected. {len(tables)} tables in public: {', '.join(tables)}")
    except Exception as exc:  # a diagnostic script: report any failure, without secrets
        print(f"Connection failed: {exception_summary(exc)}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(check())
