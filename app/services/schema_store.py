"""Persistence for indexed schema elements (pgvector on the app database).

Every function is scoped to one connection_id. Rows are removed automatically by the
database when their connection or user is deleted (ON DELETE CASCADE).
"""
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import case, delete, exists, func, insert, literal_column, select, union_all
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
    # Case-insensitive: names may come from the validator, which lowercases them.
    rows = await session.execute(
        _tables(connection_id).where(func.lower(_DISPLAY_NAME).in_({n.lower() for n in names}))
    )
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


# Hybrid search: three rankings fused with Reciprocal Rank Fusion (score = Σ 1 / (K + rank)).
# Vectors catch meaning ("revenue" ~ invoice totals), full text catches exact words that appear
# in a table's document (column names, example values), and trigram similarity of table names
# catches plurals and typos ("customers", "custmer" ~ customer).
_RRF_K = 60
_CANDIDATES_PER_RANKING = 30
_NAME_SIMILARITY_MIN = 0.4
_WORD = re.compile(r"[a-z0-9_]{3,}")
_STOPWORDS = frozenset(
    "the and for are was were what which who whom how many much show list give find all any each "
    "with from that this those these have has had our their per than then into over most more "
    "top does did can could would should total number".split()
)


def search_terms(question: str) -> str | None:
    """An OR full-text query of the question's distinctive words (safe to embed in to_tsquery)."""
    words = sorted({w for w in _WORD.findall(question.lower()) if w not in _STOPWORDS})
    return " | ".join(words) or None


async def search_tables_hybrid(
    session: AsyncSession,
    connection_id: uuid.UUID,
    question: str,
    query_vector: list[float],
    limit: int,
) -> list[TableDoc]:
    """The `limit` best tables for a question by vector + full-text + table-name similarity,
    in one SQL query. `score` is the fused RRF score (higher is better)."""
    in_connection = (SchemaElement.connection_id == connection_id, SchemaElement.kind == "table")
    distance = SchemaElement.embedding.cosine_distance(query_vector)
    rankings = [
        select(SchemaElement.id, func.row_number().over(order_by=distance).label("rank"))
        .where(*in_connection)
        .order_by(distance)
        .limit(_CANDIDATES_PER_RANKING)
    ]
    if terms := search_terms(question):
        ts_query = func.to_tsquery(literal_column("'simple'::regconfig"), terms)
        text_rank = func.ts_rank_cd(SchemaElement.search_tsv, ts_query)
        rankings.append(
            select(SchemaElement.id, func.row_number().over(order_by=text_rank.desc()).label("rank"))
            .where(*in_connection, SchemaElement.search_tsv.op("@@")(ts_query))
            .order_by(text_rank.desc())
            .limit(_CANDIDATES_PER_RANKING)
        )
    name_similarity = func.word_similarity(SchemaElement.table_name, question.lower())
    rankings.append(
        select(SchemaElement.id, func.row_number().over(order_by=name_similarity.desc()).label("rank"))
        .where(*in_connection, name_similarity > _NAME_SIMILARITY_MIN)
        .order_by(name_similarity.desc())
        .limit(_CANDIDATES_PER_RANKING)
    )

    ranked = union_all(*rankings).subquery()
    score = func.sum(1.0 / (_RRF_K + ranked.c.rank)).label("score")
    rows = await session.execute(
        select(_DISPLAY_NAME.label("table_name"), SchemaElement.doc, score)
        .join(ranked, ranked.c.id == SchemaElement.id)
        .group_by(SchemaElement.id, SchemaElement.schema_name, SchemaElement.table_name, SchemaElement.doc)
        .order_by(score.desc(), _DISPLAY_NAME)
        .limit(limit)
    )
    return [TableDoc(table_name=r.table_name, doc=r.doc, score=float(r.score)) for r in rows]
