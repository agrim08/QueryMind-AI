"""Unit tests for schema_retriever — full-schema mode and foreign-key expansion."""
import asyncio
import uuid

from app.core.ai_config import FULL_SCHEMA_CHAR_BUDGET
from app.services import schema_retriever
from app.services.schema_retriever import referenced_tables, within_budget
from app.services.schema_store import TableDoc

LINK = TableDoc(
    "UserToProject",
    "Table: UserToProject\nColumns:\n- userId (TEXT)\nForeign Keys:\n- (projectId) -> Project(id)\n- (userId) -> user(id)",
    0.61,
)
PROJECT = TableDoc("Project", "Table: Project\nColumns:\n- id (TEXT)\n- name (TEXT)", 0.64)
USER = TableDoc("user", "Table: user\nColumns:\n- id (TEXT)\n- name (TEXT)", 0.0)


class TestForeignKeyExpansion:
    def test_finds_tables_referenced_by_link_tables(self):
        assert referenced_tables([PROJECT, LINK]) == {"user"}

    def test_ignores_tables_already_present(self):
        assert referenced_tables([PROJECT, LINK, USER]) == set()

    def test_budget_keeps_order_and_always_keeps_the_first(self):
        big = TableDoc("big", "x" * 100, 1.0)
        assert [d.table_name for d in within_budget([big, PROJECT, LINK], budget=10)] == ["big"]
        assert [d.table_name for d in within_budget([PROJECT, LINK, USER], budget=10_000)] == ["Project", "UserToProject", "user"]


def _patch_store(monkeypatch, size: int, calls: list[str]):
    async def schema_size(session, connection_id):
        return size

    async def all_tables(session, connection_id):
        calls.append("all_tables")
        return [LINK, PROJECT, USER]

    async def embed_query(text):
        calls.append("embed_query")
        return [0.0] * 4

    async def search_tables_hybrid(session, connection_id, question, vector, limit):
        calls.append("search_tables")
        return [PROJECT, LINK]

    async def tables_by_name(session, connection_id, names):
        calls.append(f"tables_by_name:{sorted(names)}")
        return [USER] if "user" in names else []

    for name, fn in [("schema_size", schema_size), ("all_tables", all_tables), ("embed_query", embed_query),
                     ("search_tables_hybrid", search_tables_hybrid), ("tables_by_name", tables_by_name)]:
        monkeypatch.setattr(schema_retriever, name, fn)


class TestRetrieveSchema:
    def test_small_schema_is_sent_whole_without_embedding(self, monkeypatch):
        calls: list[str] = []
        _patch_store(monkeypatch, size=FULL_SCHEMA_CHAR_BUDGET, calls=calls)
        docs = asyncio.run(schema_retriever.retrieve_schema(None, uuid.uuid4(), "who owns project x?"))
        assert {d.table_name for d in docs} == {"UserToProject", "Project", "user"}
        assert calls == ["all_tables"]  # no embedding call, no vector search

    def test_large_schema_adds_referenced_tables(self, monkeypatch):
        calls: list[str] = []
        _patch_store(monkeypatch, size=FULL_SCHEMA_CHAR_BUDGET + 1, calls=calls)
        docs = asyncio.run(schema_retriever.retrieve_schema(None, uuid.uuid4(), "who owns project x?"))
        assert [d.table_name for d in docs] == ["Project", "UserToProject", "user"]
        assert calls == ["embed_query", "search_tables", "tables_by_name:['user']"]
