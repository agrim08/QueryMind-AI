"""Schema retriever: the tables the model sees for a question.

- Small schemas (all table documents fit FULL_SCHEMA_CHAR_BUDGET, roughly 150 tables) are sent
  whole. No embedding call or search, and no table can be missed.
- Larger schemas: the best tables by hybrid search (vector + full text + table-name
  similarity, fused in one SQL query), plus the tables they reference through foreign keys,
  so a link table always arrives with what it links to. Trimmed to the budget.

If the model still uses a table it wasn't shown, the pipeline adds that table and retries
once (query_pipeline), so a missed table costs one retry rather than a wrong answer.
"""
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_config import FULL_SCHEMA_CHAR_BUDGET, RETRIEVAL_TOP_K_TABLES
from app.services.embeddings import embed_query
from app.services.schema_store import (
    TableDoc,
    search_tables_hybrid,
    tables_by_name,
    tables_if_within,
    tables_referencing,
)

# Foreign-key lines in a table document: "- (customer_id) -> customers(id)"
_FK_TARGET = re.compile(r"^- \(.*\) -> (.+)\(.*\)$", re.MULTILINE)


def referenced_tables(docs: list[TableDoc]) -> set[str]:
    """Tables referenced by foreign keys from `docs` that aren't already in `docs`."""
    present = {d.table_name for d in docs}
    return {name for d in docs for name in _FK_TARGET.findall(d.doc)} - present


def within_budget(docs: list[TableDoc], budget: int = FULL_SCHEMA_CHAR_BUDGET) -> list[TableDoc]:
    """Keep docs in order until the character budget is spent (the first is always kept)."""
    kept, used = [], 0
    for doc in docs:
        if kept and used + len(doc.doc) > budget:
            break
        kept.append(doc)
        used += len(doc.doc)
    return kept


def bridge_tables(candidates: list[TableDoc], shown: set[str]) -> list[TableDoc]:
    """Candidates that link two or more shown tables (e.g. invoice_line between invoice and
    track, film_category between film and category): the join a question needs runs through
    them, but their own names rarely match the question."""
    return [d for d in candidates if len(set(_FK_TARGET.findall(d.doc)) & shown) >= 2]


async def with_linked_tables(
    session: AsyncSession,
    connection_id: uuid.UUID,
    ranked: list[TableDoc],
    hops: int = 1,
    bridges: bool = False,
) -> list[TableDoc]:
    """`ranked`, then (with `bridges`) tables linking two of them, then the tables all of those
    reference by foreign key, `hops` levels deep; trimmed to the budget in that order."""
    docs = list(ranked)
    if bridges:
        shown = {d.table_name for d in docs}
        docs += bridge_tables(await tables_referencing(session, connection_id, shown), shown)
    for _ in range(hops):
        linked = await tables_by_name(session, connection_id, referenced_tables(docs))
        if not linked:
            break
        docs += linked
    return within_budget(docs)


async def retrieve_schema(
    session: AsyncSession,
    connection_id: uuid.UUID,
    nl_query: str,
) -> list[TableDoc]:
    """The table documents to put in the prompt for this question."""
    if whole := await tables_if_within(session, connection_id, FULL_SCHEMA_CHAR_BUDGET):
        return whole
    return await search_schema(session, connection_id, nl_query, await embed_query(nl_query))


async def search_schema(
    session: AsyncSession, connection_id: uuid.UUID, nl_query: str, query_vector: list[float]
) -> list[TableDoc]:
    """The large-schema path: hybrid search, then the tables those link to (evals.retrieval
    measures exactly this)."""
    ranked = await search_tables_hybrid(session, connection_id, nl_query, query_vector, RETRIEVAL_TOP_K_TABLES)
    return await with_linked_tables(session, connection_id, ranked)
