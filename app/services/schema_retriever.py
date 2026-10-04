"""Schema retriever: the tables the model sees for a question.

- Small schemas (all table documents fit FULL_SCHEMA_CHAR_BUDGET) are sent whole. No
  embedding call or vector search, and no table can be missed.
- Larger schemas: the closest tables by vector search, plus the tables they reference
  through foreign keys, so a link table always arrives with what it links to.

Phase 1.4 extends the large-schema path with hybrid search and re-ranking.
"""
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_config import FULL_SCHEMA_CHAR_BUDGET, RETRIEVAL_TOP_K_TABLES
from app.services.embeddings import embed_query
from app.services.schema_store import TableDoc, all_tables, schema_size, search_tables, tables_by_name

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


async def retrieve_schema(
    session: AsyncSession,
    connection_id: uuid.UUID,
    nl_query: str,
) -> list[TableDoc]:
    """The table documents to put in the prompt for this question."""
    if await schema_size(session, connection_id) <= FULL_SCHEMA_CHAR_BUDGET:
        return await all_tables(session, connection_id)

    ranked = await search_tables(session, connection_id, await embed_query(nl_query), RETRIEVAL_TOP_K_TABLES)
    linked = await tables_by_name(session, connection_id, referenced_tables(ranked))
    return within_budget(ranked + linked)
