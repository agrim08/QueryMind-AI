"""SQL Generator — streams the model's reply from Gemini given schema context + NL query.

Yields raw text chunks as they arrive. The reply format (header lines, then SQL or another
kind of answer) is defined and parsed in app.services.reply_format.
"""
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
from app.services.prompt_context import PromptContext, render_examples, render_knowledge, render_turns
from app.services.schema_store import TableDoc

SYSTEM_PROMPT = """You are an expert PostgreSQL query writer helping non-technical people get answers from their own database.

Rules you MUST follow:
1. Reply in the REPLY FORMAT below and nothing else: raw SQL plus the listed comment lines; no markdown, no code fences, no prose.
2. Write only SELECT statements. Never use INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or any DDL/DML.
3. Use proper PostgreSQL syntax.
4. CRITICAL: You may ONLY reference tables that are explicitly listed in the "Available tables" section of the prompt.
   Never infer, guess, or join tables that are not in that list — even if a column name implies a related table exists.
5. If the question cannot be answered using ONLY the available tables and their columns, or needs knowledge the database doesn't hold, use body (e).
6. Always qualify column names when joining tables to avoid ambiguity.
7. LIMIT: If the question asks for a number of results ("top 5", "the 3 most"), use exactly that LIMIT. If it asks for the single top or bottom item ("which X has the most"), use LIMIT 1. Otherwise add LIMIT 500 only when listing individual rows, never to aggregated results.
8. CRITICAL: Always wrap ALL table names and ALL column names in double quotes (e.g., "users", "screenConfig", "projectId"). Tables listed as schema.table are written "schema"."table".
9. JOIN LOGIC: Use explicit JOINs based on the foreign keys described in the schema. If a requested column isn't on the main table, join the table that has it.
10. ALIASING: If you assign an alias to a table (e.g., "table" AS "t"), you MUST use that alias for all column references (e.g., "t"."column"). Never use the original table name if an alias exists.
11. FUZZY NAMES: When the question names a specific record by text (a project, customer, product…), the user may misremember it. Match case-insensitively on the distinctive words with ILIKE and % wildcards (e.g. "p"."name" ILIKE '%aggregator%'), not with exact equality. Use the example values listed in the schema for exact spellings.
12. LINK TABLES: To connect two tables, follow the foreign keys shown in the schema, including through link tables (e.g. UserToProject between user and Project).
13. OUTPUT COLUMNS: Return the columns the question asks for, preferring readable names over ids, with short readable aliases (e.g. AS "revenue", AS "month"). For rankings and breakdowns, return the label column first and the value being ranked by second. For trends, return the period first (date_trunc or EXTRACT), ordered by it.
14. POSTGRES DETAILS: Use EXTRACT or date_trunc for years, months and other periods. Sort rankings with DESC NULLS LAST. Cast to numeric before dividing (100.0 * a / b) and ROUND averages and percentages to 2 decimals. Use COUNT(DISTINCT ...) when counting entities across joins.

REPLY FORMAT. First these lines:
-- Intent: <number | trend | ranking | breakdown | comparison | list | record | schema | other>
-- Understood: <the question restated precisely in plain English, at most 15 words>
-- Assumption: <a choice you made, in plain English>   (0 to 2 lines, only when you had to choose)
Then exactly one body:
(a) The SQL query. After it, optionally:
    -- Instead: <the question rephrased with another reasonable reading>   (0 to 2 lines, only if you wrote an Assumption)
    -- Follow-up: <a natural next question this database can answer>   (2 or 3 lines)
(b) -- Clarify: <one short question>
    -- Option: <a reading, naming the measure or column it would use>   (2 to 4 lines)
    Ask only when reasonable readings would give materially different answers and nothing in the question or schema settles it (e.g. "best customers" could mean most spent or most orders). Otherwise answer with an Assumption and offer the other reading as Instead.
(c) -- Answer: <a plain-English answer>   for questions about the database itself (what a table or column holds, where something is stored). One or more lines, no SQL.
(d) -- Not allowed: <what they asked to change>   for requests to insert, update, delete or otherwise change data or structure.
(e) -- Cannot answer: <the reason in one plain-English sentence>
Intent meanings: number = one value or a few totals; trend = a measure over time; ranking = top or bottom N; breakdown = a measure split by category; comparison = a few named things side by side; list = matching rows; record = the details of one specific thing; schema = about the database itself.
"""


@dataclass(frozen=True)
class RetryFeedback:
    """Why the previous attempt failed, sent with the one retry (query_pipeline)."""

    previous_sql: str
    problem: str  # the database's or the validator's explanation


def _build_prompt(
    nl_query: str,
    table_docs: list[TableDoc],
    feedback: RetryFeedback | None = None,
    clarification: str | None = None,
    context: PromptContext | None = None,
) -> str:
    schema_section = "\n\n".join(doc.doc for doc in table_docs)
    # Explicitly list available tables so the model cannot claim ignorance
    available_tables = ", ".join(doc.table_name for doc in table_docs)
    clarification_section = (
        f"The user answered your clarifying question: {clarification}\n"
        "Answer the question now; don't ask another clarifying question.\n\n"
        if clarification
        else ""
    )
    retry_section = (
        f"Your previous query for this question:\n{feedback.previous_sql}\n"
        f"It failed: {feedback.problem}\n"
        "Write a corrected query.\n\n"
        if feedback
        else ""
    )
    context = context or PromptContext()
    return (
        f"Available tables (you may ONLY use these): {available_tables}\n\n"
        f"Database Schema:\n{schema_section}\n\n"
        f"{render_knowledge(context.knowledge)}"
        f"{render_examples(context.examples)}"
        f"{render_turns(context.turns)}"
        f"Question: {nl_query}\n\n"
        f"{clarification_section}"
        f"{retry_section}"
        f"SQL Query:"
    )


def build_request(
    nl_query: str,
    table_docs: list[TableDoc],
    feedback: RetryFeedback | None = None,
    clarification: str | None = None,
    context: PromptContext | None = None,
) -> tuple[str, genai_types.GenerateContentConfig]:
    """The exact prompt and config sent to Gemini (the eval suite also keys its answer cache on it)."""
    config = genai_types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=SQL_TEMPERATURE,
        max_output_tokens=SQL_MAX_OUTPUT_TOKENS,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=SQL_THINKING_BUDGET),
    )
    return _build_prompt(nl_query, table_docs, feedback, clarification, context), config


async def stream_sql(
    nl_query: str,
    table_docs: list[TableDoc],
    feedback: RetryFeedback | None = None,
    clarification: str | None = None,
    context: PromptContext | None = None,
) -> AsyncIterator[str]:
    """
    Stream the model's reply from Gemini.

    Yields text chunks as they arrive. The caller assembles the full reply and parses it
    (reply_format.parse_reply). `feedback` turns the request into a retry that shows the
    model its failed query; `clarification` carries the user's answer to a clarifying question.
    """
    contents, config = build_request(nl_query, table_docs, feedback, clarification, context)
    stream = await get_genai_client().aio.models.generate_content_stream(
        model=GENERATION_MODEL,
        contents=contents,
        config=config,
    )
    async for chunk in stream:
        if chunk.text:
            yield chunk.text
