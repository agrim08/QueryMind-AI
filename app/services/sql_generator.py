"""SQL Generator — streams SQL from Gemini given schema context + NL query.

Yields raw text chunks as they arrive from the model.
"""
from collections.abc import AsyncIterator

from google.genai import types as genai_types

from app.core.ai_config import GENERATION_MODEL
from app.services.genai_client import get_genai_client
from app.services.schema_store import TableDoc

SYSTEM_PROMPT = """You are an expert PostgreSQL query writer.

Rules you MUST follow:
1. Return ONLY the raw SQL query — no markdown, no code blocks, no explanation.
2. Write only SELECT statements. Never use INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or any DDL/DML.
3. Use proper PostgreSQL syntax.
4. CRITICAL: You may ONLY reference tables that are explicitly listed in the "Available tables" section of the prompt.
   Never infer, guess, or join tables that are not in that list — even if a column name implies a related table exists.
5. If the question cannot be answered using ONLY the available tables and their columns, return: -- Cannot answer: <reason>
6. Always qualify column names when joining tables to avoid ambiguity.
7. Use LIMIT 500 if the query could return many rows.
8. CRITICAL: Always wrap ALL table names and ALL column names in double quotes (e.g., "users", "screenConfig", "projectId").
9. JOIN LOGIC: Use explicit JOINs based on foreign keys described in the schema. If the user asks for email but a table doesn't have it, join with the "users" table.
10. ALIASING: If you assign an alias to a table (e.g., "table" AS "t"), you MUST use that alias for all column references (e.g., "t"."column"). Never use the original table name if an alias exists.
11. FUZZY NAMES: When the question names a specific record by text (a project, customer, product…), the user may misremember it. Match case-insensitively on the distinctive words with ILIKE and % wildcards (e.g. "p"."name" ILIKE '%aggregator%'), not with exact equality.
12. LINK TABLES: To connect two tables, follow the foreign keys shown in the schema, including through link tables (e.g. UserToProject between user and Project).
"""


def _build_prompt(nl_query: str, table_docs: list[TableDoc]) -> str:
    schema_section = "\n\n".join(doc.doc for doc in table_docs)
    # Explicitly list available tables so the model cannot claim ignorance
    available_tables = ", ".join(doc.table_name for doc in table_docs)
    return (
        f"Available tables (you may ONLY use these): {available_tables}\n\n"
        f"Database Schema:\n{schema_section}\n\n"
        f"Question: {nl_query}\n\n"
        f"SQL Query:"
    )


async def stream_sql(
    nl_query: str,
    table_docs: list[TableDoc],
) -> AsyncIterator[str]:
    """
    Stream SQL tokens from Gemini.

    Yields text chunks as they arrive. The caller is responsible for
    assembling the full SQL string for validation.
    """
    stream = await get_genai_client().aio.models.generate_content_stream(
        model=GENERATION_MODEL,
        contents=_build_prompt(nl_query, table_docs),
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.1,
            max_output_tokens=1024,
        ),
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text
