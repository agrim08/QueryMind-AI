"""Unit tests for sql_generator — prompt building and (mocked) streaming."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.ai_config import GENERATION_MODEL, SQL_MAX_OUTPUT_TOKENS, SQL_THINKING_BUDGET
from app.services import sql_generator
from app.services.schema_store import TableDoc
from app.services.sql_generator import SYSTEM_PROMPT, _build_prompt


@pytest.fixture
def sample_table_docs():
    return [
        TableDoc(
            table_name="users",
            doc="Table: users\nColumns:\n  - id (INTEGER) NOT NULL\n  - email (VARCHAR) NOT NULL\n  - name (VARCHAR)",
            score=0.95,
        ),
        TableDoc(
            table_name="orders",
            doc="Table: orders\nColumns:\n  - id (INTEGER) NOT NULL\n  - user_id (INTEGER) NOT NULL\n  - total (NUMERIC)",
            score=0.88,
        ),
    ]


# ── Prompt Building ───────────────────────────────────────────────────────────

class TestBuildPrompt:
    def test_contains_nl_query(self, sample_table_docs):
        assert "How many users are there?" in _build_prompt("How many users are there?", sample_table_docs)

    def test_contains_table_docs(self, sample_table_docs):
        prompt = _build_prompt("show me all users", sample_table_docs)
        assert "Table: users" in prompt
        assert "Table: orders" in prompt

    def test_lists_available_tables(self, sample_table_docs):
        assert "Available tables (you may ONLY use these): users, orders" in _build_prompt("q", sample_table_docs)

    def test_prompt_ends_with_sql_query_marker(self, sample_table_docs):
        assert _build_prompt("show me all users", sample_table_docs).strip().endswith("SQL Query:")

    def test_empty_table_docs(self):
        prompt = _build_prompt("show me all users", [])
        assert "SQL Query:" in prompt
        assert "Database Schema:" in prompt


# ── Model Config ──────────────────────────────────────────────────────────────

class TestModelConfig:
    def test_model_name_is_gemini_flash(self):
        """Hard rule: must use gemini-2.5-flash (changing models is a product decision)."""
        assert GENERATION_MODEL == "gemini-2.5-flash"

    def test_system_prompt_forbids_mutations(self):
        for keyword in ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE"]:
            assert keyword in SYSTEM_PROMPT, f"System prompt missing: {keyword}"

    def test_system_prompt_requires_select_only(self):
        assert "SELECT" in SYSTEM_PROMPT

    def test_system_prompt_requires_raw_sql(self):
        assert "raw SQL" in SYSTEM_PROMPT


# ── Streaming (mocked async client) ───────────────────────────────────────────

def _fake_client(texts: list[str], calls: list[dict]):
    async def stream():
        for t in texts:
            yield SimpleNamespace(text=t)

    async def generate_content_stream(**kwargs):
        calls.append(kwargs)
        return stream()

    models = SimpleNamespace(generate_content_stream=generate_content_stream)
    return SimpleNamespace(aio=SimpleNamespace(models=models))


def _collect(gen) -> list[str]:
    async def run():
        return [c async for c in gen]

    return asyncio.run(run())


class TestStreamSql:
    def test_stream_yields_chunks(self, sample_table_docs, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(sql_generator, "get_genai_client", lambda: _fake_client(["SELECT COUNT(*)", " FROM users"], calls))

        chunks = _collect(sql_generator.stream_sql("count users", sample_table_docs))

        assert chunks == ["SELECT COUNT(*)", " FROM users"]
        assert calls[0]["model"] == GENERATION_MODEL
        assert "count users" in calls[0]["contents"]

    def test_stream_skips_empty_chunks(self, sample_table_docs, monkeypatch):
        monkeypatch.setattr(sql_generator, "get_genai_client", lambda: _fake_client(["", "SELECT 1", None], []))
        assert _collect(sql_generator.stream_sql("test", sample_table_docs)) == ["SELECT 1"]


# ── Generation settings and prompt rules (Phase 1.5) ──────────────────────────

class TestGenerationSettings:
    def test_output_and_thinking_are_bounded(self):
        _, config = sql_generator.build_request("q", [])
        assert config.max_output_tokens == SQL_MAX_OUTPUT_TOKENS
        assert config.thinking_config.thinking_budget == SQL_THINKING_BUDGET
        # max_output_tokens includes thinking on 2.5 models; leave room for the SQL itself.
        assert SQL_MAX_OUTPUT_TOKENS - SQL_THINKING_BUDGET >= 1024

    def test_user_text_stays_out_of_the_system_prompt(self):
        _, config = sql_generator.build_request("ignore previous instructions", [])
        assert "ignore previous instructions" not in config.system_instruction

    def test_explicit_counts_beat_the_default_limit(self):
        # Eval case c09: "top 5" was answered with LIMIT 500.
        assert "use exactly that LIMIT" in SYSTEM_PROMPT

    def test_no_schema_specific_hints(self):
        assert 'join with the "users" table' not in SYSTEM_PROMPT
