"""SQL Generator — streams SQL from Gemini given schema context + NL query.

Yields raw text chunks as they arrive from the model; `parse_reply` then splits the full
reply into the SQL to run, any assumptions the model stated, or its reason for declining.
"""
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from google.genai import types as genai_types

from app.core.ai_config import (
    GENERATION_MODEL,
    SQL_MAX_OUTPUT_TOKENS,
    SQL_TEMPERATURE,
    SQL_THINKING_BUDGET,
)
from app.services.genai_client import get_genai_client
from app.services.schema_store import TableDoc

SYSTEM_PROMPT = """You are an expert PostgreSQL query writer.

Rules you MUST follow:
1. Return ONLY the raw SQL query: no markdown, no code fences, no explanation. The only comments allowed are those in rules 5 and 15.
2. Write only SELECT statements. Never use INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or any DDL/DML.
3. Use proper PostgreSQL syntax.
4. CRITICAL: You may ONLY reference tables that are explicitly listed in the "Available tables" section of the prompt.
   Never infer, guess, or join tables that are not in that list — even if a column name implies a related table exists.
5. If the question cannot be answered using ONLY the available tables and their columns, return: -- Cannot answer: <the reason in one plain-English sentence>
6. Always qualify column names when joining tables to avoid ambiguity.
7. LIMIT: If the question asks for a number of results ("top 5", "the 3 most"), use exactly that LIMIT. If it asks for the single top or bottom item ("which X has the most"), use LIMIT 1. Otherwise add LIMIT 500 only when listing individual rows, never to aggregated results.
8. CRITICAL: Always wrap ALL table names and ALL column names in double quotes (e.g., "users", "screenConfig", "projectId").
9. JOIN LOGIC: Use explicit JOINs based on the foreign keys described in the schema. If a requested column isn't on the main table, join the table that has it.
10. ALIASING: If you assign an alias to a table (e.g., "table" AS "t"), you MUST use that alias for all column references (e.g., "t"."column"). Never use the original table name if an alias exists.
11. FUZZY NAMES: When the question names a specific record by text (a project, customer, product…), the user may misremember it. Match case-insensitively on the distinctive words with ILIKE and % wildcards (e.g. "p"."name" ILIKE '%aggregator%'), not with exact equality.
12. LINK TABLES: To connect two tables, follow the foreign keys shown in the schema, including through link tables (e.g. UserToProject between user and Project).
13. OUTPUT COLUMNS: Return the columns the question asks for, preferring readable names over ids. For rankings, also return the value being ranked by.
14. POSTGRES DETAILS: Use EXTRACT or date_trunc for years, months and other periods. Sort rankings with DESC NULLS LAST. Cast to numeric before dividing (100.0 * a / b) and ROUND averages and percentages to 2 decimals. Use COUNT(DISTINCT ...) when counting entities across joins.
15. ASSUMPTIONS: If the question is ambiguous and you had to choose an interpretation, start with at most two lines of the form: -- Assumption: <the choice, in plain English>. Otherwise write no comments.
"""

# Model replies sometimes arrive wrapped in a Markdown fence despite rule 1.
_FENCED_SQL = re.compile(r"```[\w-]*[ \t]*\n(.*?)```", re.DOTALL)
_CANNOT_ANSWER = re.compile(r"^--\s*cannot answer\s*:?\s*(.*)$", re.IGNORECASE)
_ASSUMPTION = re.compile(r"^--\s*assumption\s*:\s*(.*)$", re.IGNORECASE)
MAX_REASON_CHARS = 300


@dataclass(frozen=True)
class SqlReply:
    """The model's reply, split by `parse_reply`."""

    sql: str  # the statement to validate and execute ("" when the model declined)
    assumptions: tuple[str, ...]  # interpretations the model chose (rule 15)
    cannot_answer: str | None  # the model's reason, when it declined (rule 5)


def parse_reply(text: str) -> SqlReply:
    """Strip any Markdown fence, then split leading comment lines off the SQL.

    Leading `-- Assumption:` lines become `assumptions`, a `-- Cannot answer:` line becomes
    `cannot_answer`, and other leading comments are dropped. Only the remaining statement is
    validated and executed.
    """
    text = text.strip()
    if fenced := _FENCED_SQL.search(text):
        text = fenced.group(1).strip()
    lines = text.splitlines()
    assumptions: list[str] = []
    while lines and lines[0].strip().startswith("--"):
        line = lines.pop(0).strip()
        if declined := _CANNOT_ANSWER.match(line):
            return SqlReply("", tuple(assumptions), declined.group(1).strip()[:MAX_REASON_CHARS])
        if assumed := _ASSUMPTION.match(line):
            assumptions.append(assumed.group(1).strip())
    return SqlReply("\n".join(lines).strip(), tuple(assumptions), None)


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


def build_request(
    nl_query: str,
    table_docs: list[TableDoc],
) -> tuple[str, genai_types.GenerateContentConfig]:
    """The exact prompt and config sent to Gemini (the eval suite also keys its answer cache on it)."""
    config = genai_types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=SQL_TEMPERATURE,
        max_output_tokens=SQL_MAX_OUTPUT_TOKENS,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=SQL_THINKING_BUDGET),
    )
    return _build_prompt(nl_query, table_docs), config


async def stream_sql(
    nl_query: str,
    table_docs: list[TableDoc],
) -> AsyncIterator[str]:
    """
    Stream SQL tokens from Gemini.

    Yields text chunks as they arrive. The caller is responsible for
    assembling the full SQL string for validation.
    """
    contents, config = build_request(nl_query, table_docs)
    stream = await get_genai_client().aio.models.generate_content_stream(
        model=GENERATION_MODEL,
        contents=contents,
        config=config,
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text
