"""Schema indexer: introspect a target database, embed one document per table, and
store the vectors in pgvector (schema_elements).

`index_connection` yields progress events for the SSE stream:
  {"type": "status",   "message": "..."}
  {"type": "progress", "current": N, "total": M}
  {"type": "done",     "table_count": N}
  {"type": "error",    "message": "..."}   # always user-safe

A connection is marked indexed only after every table has been embedded and the new
rows have replaced the old ones in a single transaction.
"""
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, inspect, or_, text, update

from app.core import errors
from app.core.ai_config import EMBED_BATCH_SIZE
from app.core.exceptions import InvalidInput
from app.db.session import AsyncSessionLocal
from app.models.models import DBConnection
from app.services.embeddings import embed_texts
from app.services.schema_store import NewElement, replace_elements
from app.services.target_db import decrypt_url, open_target_engine

logger = logging.getLogger(__name__)

# A claim older than this is treated as abandoned (e.g. the worker crashed mid-run).
CLAIM_TTL = timedelta(minutes=15)
SAMPLE_VALUE_MAX_CHARS = 64


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class ForeignKeyInfo:
    columns: list[str]
    referred_table: str
    referred_columns: list[str]


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKeyInfo]
    sample: dict[str, object] = field(default_factory=dict)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build_table_doc(table: TableInfo) -> str:
    """Text that represents a table for embedding and for the generation prompt."""
    lines = [f"Table: {table.name}", "Columns:"]
    for col in table.columns:
        sample = table.sample.get(col.name)
        sample_text = f" (e.g. {str(sample)[:SAMPLE_VALUE_MAX_CHARS]})" if sample not in (None, "") else ""
        not_null = "" if col.nullable else " NOT NULL"
        lines.append(f"- {col.name} ({col.type}){not_null}{sample_text}")
    if table.foreign_keys:
        lines.append("Foreign Keys:")
        for fk in table.foreign_keys:
            lines.append(
                f"- ({', '.join(fk.columns)}) -> {fk.referred_table}({', '.join(fk.referred_columns)})"
            )
    return "\n".join(lines)


async def introspect(url: str) -> list[TableInfo]:
    """Tables, columns, foreign keys and one sample row per table (public schema)."""

    def _inspect(sync_conn) -> list[TableInfo]:
        inspector = inspect(sync_conn)
        return [
            TableInfo(
                name=name,
                columns=[
                    ColumnInfo(c["name"], str(c["type"]), c.get("nullable", True))
                    for c in inspector.get_columns(name)
                ],
                foreign_keys=[
                    ForeignKeyInfo(fk["constrained_columns"], fk["referred_table"], fk["referred_columns"])
                    for fk in inspector.get_foreign_keys(name)
                ],
            )
            for name in inspector.get_table_names()
        ]

    async with open_target_engine(url) as engine, engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        tables = await conn.run_sync(_inspect)
        for table in tables:
            try:
                async with conn.begin_nested():  # a failing sample must not abort the transaction
                    row = (
                        await conn.execute(text(f"SELECT * FROM {_quote_ident(table.name)} LIMIT 1"))
                    ).mappings().first()
                table.sample = dict(row) if row else {}
            except Exception as exc:
                logger.info("Sample row skipped for %s: %s", table.name, type(exc).__name__)
        return tables


async def _claim(connection_id: uuid.UUID) -> bool:
    """Atomically mark the connection as being indexed. False if another run holds it."""
    async with AsyncSessionLocal() as session:
        claimed = await session.execute(
            update(DBConnection)
            .where(
                DBConnection.id == connection_id,
                or_(
                    DBConnection.indexing_started_at.is_(None),
                    DBConnection.indexing_started_at < func.now() - CLAIM_TTL,
                ),
            )
            .values(indexing_started_at=func.now())
            .returning(DBConnection.id)
        )
        won = claimed.first() is not None
        await session.commit()
        return won


async def _release_claim(connection_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(DBConnection).where(DBConnection.id == connection_id).values(indexing_started_at=None)
        )
        await session.commit()


async def index_connection(
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    encrypted_url: str,
) -> AsyncIterator[dict]:
    """Index a connection, holding its indexing claim for the duration of the run.

    The claim is taken here, inside the stream, rather than by the caller: if the client
    disconnects before streaming starts this generator never runs and nothing is held;
    if it disconnects mid-run, the generator is closed and `finally` releases the claim.
    A claim left by a crashed worker expires after CLAIM_TTL.
    """
    claimed = completed = False
    reading_target = True  # failures before introspection completes are the target DB's
    try:
        claimed = await _claim(connection_id)
        if not claimed:
            yield {"type": "error", "message": errors.INDEXING_IN_PROGRESS}
            return

        yield {"type": "status", "message": "Connecting to database..."}
        tables = await introspect(decrypt_url(encrypted_url))
        reading_target = False

        total = len(tables)
        yield {"type": "status", "message": f"Found {total} tables. Building embeddings..."}
        docs = [build_table_doc(t) for t in tables]
        vectors: list[list[float]] = []
        for start in range(0, total, EMBED_BATCH_SIZE):
            vectors += await embed_texts(docs[start : start + EMBED_BATCH_SIZE], "RETRIEVAL_DOCUMENT")
            yield {"type": "progress", "current": len(vectors), "total": total}

        yield {"type": "status", "message": "Updating search index..."}
        elements = [
            NewElement(kind="table", schema_name="public", table_name=t.name, doc=doc, embedding=vec)
            for t, doc, vec in zip(tables, docs, vectors)
        ]
        async with AsyncSessionLocal() as session:
            await replace_elements(session, connection_id, user_id, elements)
            await session.execute(
                update(DBConnection)
                .where(DBConnection.id == connection_id)
                .values(indexed_at=func.now(), table_count=total, indexing_started_at=None)
            )
            await session.commit()
        completed = True
        yield {"type": "done", "table_count": total}

    except InvalidInput as exc:
        yield {"type": "error", "message": exc.message}
    except Exception as exc:
        if reading_target:
            logger.warning("Schema inspection failed: %s", errors.exception_summary(exc))
            message = errors.describe_connection_error(exc)
        else:
            logger.exception("Schema indexing failed for connection %s", connection_id)
            message = errors.INDEXING_FAILED
        yield {"type": "error", "message": message}
    finally:
        if claimed and not completed:
            await _release_claim(connection_id)
