"""Unit tests for schema_retriever — full-schema mode and foreign-key expansion."""
import asyncio
import uuid

from app.core.ai_config import FULL_SCHEMA_CHAR_BUDGET
from app.services import schema_retriever
from app.services.schema_retriever import bridge_tables, referenced_tables, within_budget
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
    async def tables_if_within(session, connection_id, budget):
        calls.append("tables_if_within")
        return [LINK, PROJECT, USER] if size <= budget else []

    async def embed_query(text):
        calls.append("embed_query")
        return [0.0] * 4

    async def search_tables_hybrid(session, connection_id, question, vector, limit):
        calls.append("search_tables")
        return [PROJECT, LINK]

    async def tables_by_name(session, connection_id, names):
        calls.append(f"tables_by_name:{sorted(names)}")
        return [USER] if "user" in names else []

    for name, fn in [("tables_if_within", tables_if_within), ("embed_query", embed_query),
                     ("search_tables_hybrid", search_tables_hybrid), ("tables_by_name", tables_by_name)]:
        monkeypatch.setattr(schema_retriever, name, fn)


class TestRetrieveSchema:
    def test_small_schema_is_sent_whole_without_embedding(self, monkeypatch):
        calls: list[str] = []
        _patch_store(monkeypatch, size=FULL_SCHEMA_CHAR_BUDGET, calls=calls)
        docs = asyncio.run(schema_retriever.retrieve_schema(None, uuid.uuid4(), "who owns project x?"))
        assert {d.table_name for d in docs} == {"UserToProject", "Project", "user"}
        assert calls == ["tables_if_within"]  # one query: no embedding call, no vector search

    def test_large_schema_adds_referenced_tables(self, monkeypatch):
        calls: list[str] = []
        _patch_store(monkeypatch, size=FULL_SCHEMA_CHAR_BUDGET + 1, calls=calls)
        docs = asyncio.run(schema_retriever.retrieve_schema(None, uuid.uuid4(), "who owns project x?"))
        assert [d.table_name for d in docs] == ["Project", "UserToProject", "user"]
        assert calls == ["tables_if_within", "embed_query", "search_tables", "tables_by_name:['user']"]


INVOICE = TableDoc("invoice", "Table: invoice\nColumns:\n- id (int)\n- customer_id (int)\nForeign Keys:\n- (customer_id) -> customer(id)", 0.9)
TRACK = TableDoc("track", "Table: track\nColumns:\n- id (int)\n- genre_id (int)\nForeign Keys:\n- (genre_id) -> genre(id)", 0.8)
INVOICE_LINE = TableDoc(
    "invoice_line",
    "Table: invoice_line\nForeign Keys:\n- (invoice_id) -> invoice(id)\n- (track_id) -> track(id)",
    0.0,
)
ARCHIVE = TableDoc("legacy.invoice_archive", "Table: legacy.invoice_archive\nForeign Keys:\n- (invoice_id) -> invoice(id)", 0.0)


class TestBridgesAndHops:
    def test_a_bridge_links_two_shown_tables(self):
        assert bridge_tables([INVOICE_LINE, ARCHIVE], {"invoice", "track"}) == [INVOICE_LINE]

    def test_a_table_linking_one_shown_table_is_not_a_bridge(self):
        assert bridge_tables([ARCHIVE], {"invoice", "track"}) == []

    def test_bridges_then_two_hops(self, monkeypatch):
        names_asked: list[set[str]] = []

        async def tables_referencing(session, connection_id, names):
            return [INVOICE_LINE, ARCHIVE]

        async def tables_by_name(session, connection_id, names):
            names_asked.append(set(names))
            catalog = {"customer": TableDoc("customer", "Table: customer\nForeign Keys:\n- (support_rep_id) -> employee(id)", 0.0),
                       "genre": TableDoc("genre", "Table: genre", 0.0),
                       "employee": TableDoc("employee", "Table: employee", 0.0)}
            return [catalog[n] for n in sorted(names) if n in catalog]

        monkeypatch.setattr(schema_retriever, "tables_referencing", tables_referencing)
        monkeypatch.setattr(schema_retriever, "tables_by_name", tables_by_name)
        docs = asyncio.run(schema_retriever.with_linked_tables(None, uuid.uuid4(), [INVOICE, TRACK], hops=2, bridges=True))

        assert [d.table_name for d in docs] == ["invoice", "track", "invoice_line", "customer", "genre", "employee"]
        assert names_asked == [{"customer", "genre"}, {"employee"}]  # hop 1, then hop 2
