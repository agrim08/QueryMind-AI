"""Schema Indexer — inspects a Postgres DB, builds table docs, embeds them, upserts to Pinecone.

Streams progress events as an async generator of SSE-compatible strings.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator

from google import genai
from google.genai import types as genai_types
from pinecone import Pinecone
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.security import decrypt

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "models/gemini-embedding-002"
EMBEDDING_DIMENSIONS = 3072


def _build_table_doc(table_info: dict) -> str:
    """Create a text representation of a table for indexing."""
    name = table_info["table_name"]
    cols = table_info["columns"]
    fks = table_info.get("foreign_keys", [])
    sample_values = table_info.get("sample", {})

    lines = [f"Table: {name}"]
    lines.append("Columns:")
    for c in cols:
        sample = sample_values.get(c["name"], "")
        sample_str = f" (e.g. {sample})" if sample else ""
        lines.append(f"- {c['name']} ({c['type']}){' NOT NULL' if not c['nullable'] else ''}{sample_str}")
    
    if fks:
        lines.append("Foreign Keys:")
        for fk in fks:
            src = ", ".join(fk["constrained_columns"])
            dst_table = fk["referred_table"]
            dst_cols = ", ".join(fk["referred_columns"])
            lines.append(f"- ({src}) -> {dst_table}({dst_cols})")

    return "\n".join(lines)


async def _inspect_schema(conn_string: str) -> list[dict]:
    """Use SQLAlchemy inspect() to extract table/column metadata."""
    engine = create_async_engine(conn_string, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            # Run synchronous inspect in executor
            def _sync_inspect(sync_conn):
                inspector = inspect(sync_conn)
                tables = []
                for table_name in inspector.get_table_names():
                    columns = []
                    for col in inspector.get_columns(table_name):
                        columns.append(
                            {
                                "name": col["name"],
                                "type": str(col["type"]),
                                "nullable": col.get("nullable", True),
                            }
                        )
                    
                    # Extract Foreign Keys
                    fks = []
                    for fk in inspector.get_foreign_keys(table_name):
                        fks.append({
                            "constrained_columns": fk["constrained_columns"],
                            "referred_table": fk["referred_table"],
                            "referred_columns": fk["referred_columns"]
                        })
                        
                    tables.append({
                        "table_name": table_name, 
                        "columns": columns,
                        "foreign_keys": fks
                    })
                return tables

            tables = await conn.run_sync(_sync_inspect)

            # Fetch one sample row per table for richer embeddings
            for table in tables:
                try:
                    result = await conn.execute(
                        text(f'SELECT * FROM "{table["table_name"]}" LIMIT 1')
                    )
                    row = result.mappings().first()
                    table["sample"] = dict(row) if row else {}
                except Exception:
                    table["sample"] = {}

        return tables
    finally:
        await engine.dispose()


async def index_schema(
    encrypted_conn_string: str,
    namespace: str,
) -> AsyncGenerator[str, None]:
    """
    Full indexing pipeline — yields SSE event strings.

    Events:
      data: {"type": "status", "message": "..."}
      data: {"type": "progress", "current": N, "total": M}
      data: {"type": "done", "table_count": N}
      data: {"type": "error", "message": "..."}
    """

    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    try:
        from app.api.endpoints.connections import _to_asyncpg  # avoid circular

        # 1. Decrypt connection string & normalize
        conn_string = decrypt(encrypted_conn_string)
        conn_string = _to_asyncpg(conn_string)

        yield _sse({"type": "status", "message": "Connecting to database..."})

        # 2. Inspect schema
        tables = await _inspect_schema(conn_string)
        total = len(tables)

        if total == 0:
            yield _sse({"type": "status", "message": "No tables found."})
            # Clear any old garbage even if empty
            pc = Pinecone(api_key=settings.PINECONE_API_KEY)
            index = pc.Index(settings.PINECONE_INDEX_NAME)
            try:
                index.delete(delete_all=True, namespace=namespace)
            except Exception:
                pass
            yield _sse({"type": "done", "table_count": 0})
            return

        yield _sse({"type": "status", "message": f"Found {total} tables. Building embeddings..."})

        # 3. Build table docs
        docs = []
        for table in tables:
            doc = _build_table_doc(table)
            docs.append({
                "table_name": table["table_name"],
                "doc": doc,
                "columns": table["columns"]
            })

        # 4. Batch embed all docs in ONE API call (per spec)
        yield _sse({"type": "status", "message": "Generating embeddings..."})
        client = genai.Client(api_key=settings.GOOGLE_API_KEY)
        embed_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=[d["doc"] for d in docs],
                config=genai_types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
            ),
        )
        embeddings = [e.values for e in embed_result.embeddings]

        # 5. Upsert to Pinecone
        yield _sse({"type": "status", "message": "Updating vector index..."})
        pc = Pinecone(api_key=settings.PINECONE_API_KEY)
        index = pc.Index(settings.PINECONE_INDEX_NAME)

        # CRITICAL: Clear old vectors first to prevent "ghost" tables (e.g. dropped tables)
        # from lingering and causing hallucinations.
        try:
            index.delete(delete_all=True, namespace=namespace)
        except Exception as e:
            # If namespace doesn't exist, this might fail or warn, proceed anyway
            pass

        vectors = []
        for i, (doc_meta, embedding) in enumerate(zip(docs, embeddings)):
            vectors.append(
                {
                    "id": f"{namespace}::{doc_meta['table_name']}",
                    "values": embedding,
                    "metadata": {
                        "table_name": doc_meta["table_name"],
                        "doc": doc_meta["doc"],
                        "namespace": namespace,
                        "columns": json.dumps([c["name"] for c in doc_meta["columns"]]),
                    },
                }
            )

        # Upsert in batches of 100
        batch_size = 100
        total_vectors = len(vectors)
        for i in range(0, total_vectors, batch_size):
            batch = vectors[i : i + batch_size]
            index.upsert(vectors=batch, namespace=namespace)
            yield _sse({"type": "progress", "current": min(i + batch_size, total_vectors), "total": total})

        yield _sse({"type": "done", "table_count": total})

    except Exception as exc:
        yield _sse({"type": "error", "message": str(exc)})
