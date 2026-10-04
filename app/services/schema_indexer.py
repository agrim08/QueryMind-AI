"""Schema indexer: introspect a target database, embed one document per table or view,
and store the vectors in pgvector (schema_elements).

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
from datetime import timedelta

from sqlalchemy import func, or_, update

from app.core import errors
from app.core.ai_config import EMBED_BATCH_SIZE, MAX_INDEXED_TABLES
from app.core.exceptions import InvalidInput
from app.db.session import AsyncSessionLocal
from app.models.models import DBConnection
from app.services.embeddings import embed_texts
from app.services.schema_introspection import TableInfo, introspect
from app.services.schema_store import NewElement, replace_elements
from app.services.target_db import decrypt_url

logger = logging.getLogger(__name__)

# A claim older than this is treated as abandoned (e.g. the worker crashed mid-run).
CLAIM_TTL = timedelta(minutes=15)

_VIEW_KINDS = {"v": "View", "m": "Materialized view", "f": "Foreign table"}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_table_doc(table: TableInfo) -> str:
    """Text that represents a table or view, for embedding and for the generation prompt.

    Deterministic: the same schema always produces the same text. Foreign-key lines keep the
    form `- (cols) -> table(cols)`, which schema_retriever parses for FK expansion.
    """
    header = f"{_VIEW_KINDS.get(table.kind, 'Table')}: {table.display_name}"
    if table.row_estimate >= 0 and table.kind in ("r", "p", "m"):
        header += f" (~{table.row_estimate:,} rows)"
    lines = [header]
    if table.comment:
        lines.append(f"Description: {table.comment}")
    lines.append("Columns:")
    for col in table.columns:
        parts = [f"- {col.name} ({col.type})"]
        if col.primary_key:
            parts.append("PRIMARY KEY")
        elif not col.nullable:
            parts.append("NOT NULL")
        if col.values:
            more = f" (+{col.more_values} more)" if col.more_values else ""
            parts.append(f"values: {', '.join(_sql_literal(v) for v in col.values)}{more}")
        if col.comment:
            parts.append(f"-- {col.comment}")
        lines.append(" ".join(parts))
    if table.foreign_keys:
        lines.append("Foreign Keys:")
        for fk in table.foreign_keys:
            lines.append(
                f"- ({', '.join(fk.columns)}) -> {fk.referred_table}({', '.join(fk.referred_columns)})"
            )
    return "\n".join(lines)


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
        schema = await introspect(decrypt_url(encrypted_url))
        tables = schema.tables
        reading_target = False

        total = len(tables)
        found = f"Found {total} tables and views"
        if schema.truncated:
            found += f" (indexing the {MAX_INDEXED_TABLES:,} largest; more exist)"
        yield {"type": "status", "message": f"{found}. Building embeddings..."}
        docs = [build_table_doc(t) for t in tables]
        vectors: list[list[float]] = []
        for start in range(0, total, EMBED_BATCH_SIZE):
            vectors += await embed_texts(docs[start : start + EMBED_BATCH_SIZE], "RETRIEVAL_DOCUMENT")
            yield {"type": "progress", "current": len(vectors), "total": total}

        yield {"type": "status", "message": "Updating search index..."}
        elements = [
            NewElement(kind="table", schema_name=t.schema, table_name=t.name, doc=doc, embedding=vec)
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
