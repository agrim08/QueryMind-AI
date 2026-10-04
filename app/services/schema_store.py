"""Persistence for indexed schema elements (pgvector on the app database).

Every function is scoped to one connection_id. Rows are removed automatically by the
database when their connection or user is deleted (ON DELETE CASCADE).
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import case, delete, exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import SchemaElement


def display_name(schema_name: str, table_name: str) -> str:
    """How a table is named in documents, prompts and the validator: bare in `public`,
    schema-qualified elsewhere (e.g. `sales.orders`)."""
    return table_name if schema_name == "public" else f"{schema_name}.{table_name}"


# The same rule in SQL, so lookups by name and the names returned agree with display_name().
_DISPLAY_NAME = case(
    (SchemaElement.schema_name == "public", SchemaElement.table_name),
    else_=SchemaElement.schema_name + "." + SchemaElement.table_name,
)


@dataclass(frozen=True)
class NewElement:
    kind: str  # "table" | "column"
    schema_name: str
    table_name: str
    doc: str
    embedding: list[float]
    column_name: str | None = None


@dataclass(frozen=True)
class TableDoc:
    """A retrieved table and its document, ranked by similarity to the question."""

    table_name: str  # display name: bare in `public`, schema-qualified elsewhere
    doc: str
    score: float


async def replace_elements(
    session: AsyncSession,
    connection_id: uuid.UUID,
    user_id: uuid.UUID,
    elements: list[NewElement],
) -> None:
    """Swap a connection's elements for a fresh set. The caller commits, so a failed
    re-index never leaves a half-written index behind."""
    await session.execute(delete(SchemaElement).where(SchemaElement.connection_id == connection_id))
    if elements:
        await session.execute(
            insert(SchemaElement),
            [
                {
                    "connection_id": connection_id,
                    "user_id": user_id,
                    "kind": e.kind,
                    "schema_name": e.schema_name,
                    "table_name": e.table_name,
                    "column_name": e.column_name,
                    "doc": e.doc,
                    "embedding": e.embedding,
                }
                for e in elements
            ],
        )


async def has_elements(session: AsyncSession, connection_id: uuid.UUID) -> bool:
    """True once the connection has been indexed into pgvector."""
    return bool(
        await session.scalar(select(exists().where(SchemaElement.connection_id == connection_id)))
    )


def _tables(connection_id: uuid.UUID):
    return select(_DISPLAY_NAME.label("table_name"), SchemaElement.doc).where(
        SchemaElement.connection_id == connection_id, SchemaElement.kind == "table"
    )


async def schema_size(session: AsyncSession, connection_id: uuid.UUID) -> int:
    """Total characters of all table documents (decides full-schema vs. retrieval)."""
    return await session.scalar(
        select(func.coalesce(func.sum(func.length(SchemaElement.doc)), 0)).where(
            SchemaElement.connection_id == connection_id, SchemaElement.kind == "table"
        )
    ) or 0


async def all_tables(session: AsyncSession, connection_id: uuid.UUID) -> list[TableDoc]:
    rows = await session.execute(_tables(connection_id).order_by(_DISPLAY_NAME))
    return [TableDoc(table_name=r.table_name, doc=r.doc, score=1.0) for r in rows]


async def tables_by_name(session: AsyncSession, connection_id: uuid.UUID, names: set[str]) -> list[TableDoc]:
    if not names:
        return []
    rows = await session.execute(_tables(connection_id).where(_DISPLAY_NAME.in_(names)))
    return [TableDoc(table_name=r.table_name, doc=r.doc, score=0.0) for r in rows]


async def search_tables(
    session: AsyncSession,
    connection_id: uuid.UUID,
    query_vector: list[float],
    limit: int,
) -> list[TableDoc]:
    """Nearest tables by cosine distance. Exact search: a connection has at most a few
    thousand rows, so an ANN index isn't needed at this scale."""
    distance = SchemaElement.embedding.cosine_distance(query_vector)
    rows = await session.execute(
        select(_DISPLAY_NAME.label("table_name"), SchemaElement.doc, distance.label("distance"))
        .where(SchemaElement.connection_id == connection_id, SchemaElement.kind == "table")
        .order_by(distance)
        .limit(limit)
    )
    return [TableDoc(table_name=r.table_name, doc=r.doc, score=1.0 - r.distance) for r in rows]
