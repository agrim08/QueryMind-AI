"""Unit tests for schema introspection helpers and table documents (Phase 1.3)."""
import asyncio
import os

import pytest

from app.core.config import settings
from app.services.schema_indexer import build_table_doc
from app.services.schema_introspection import (
    SHOWN_VALUES,
    ColumnInfo,
    ForeignKeyInfo,
    TableInfo,
    introspect,
    is_sensitive_column,
    quote_ident,
    short_type,
    values_from_rows,
    values_from_statistics,
)
from app.services.schema_retriever import referenced_tables
from app.services.schema_store import TableDoc, display_name
from app.services.target_db import normalize_url


class TestTableDoc:
    def test_full_document(self):
        table = TableInfo(
            schema="public",
            name="orders",
            kind="r",
            row_estimate=12_000,
            comment="One row per checkout",
            columns=[
                ColumnInfo("id", "uuid", False, primary_key=True),
                ColumnInfo("status", "text", False, values=("paid", "it's late"), more_values=3),
                ColumnInfo("total", "numeric", True, comment="Including tax"),
            ],
            foreign_keys=[ForeignKeyInfo(["customer_id"], "sales.customers", ["id"])],
        )
        assert build_table_doc(table) == (
            "Table: orders (~12,000 rows)\n"
            "Description: One row per checkout\n"
            "Columns:\n"
            "- id (uuid) PRIMARY KEY\n"
            "- status (text) NOT NULL values: 'paid', 'it''s late' (+3 more)\n"
            "- total (numeric) -- Including tax\n"
            "Foreign Keys:\n"
            "- (customer_id) -> sales.customers(id)"
        )

    def test_views_are_labelled_and_have_no_row_count(self):
        view = TableInfo(schema="reporting", name="sales", kind="v", row_estimate=0, columns=[ColumnInfo("total", "numeric", True)])
        assert build_table_doc(view).startswith("View: reporting.sales\nColumns:")

    def test_unknown_row_count_is_omitted(self):
        table = TableInfo(schema="public", name="t", kind="r", row_estimate=-1, columns=[])
        assert build_table_doc(table).splitlines()[0] == "Table: t"

    def test_an_empty_table_is_marked_so_search_ranks_it_lower(self):
        table = TableInfo(schema="legacy", name="payment_archive", kind="r", row_estimate=0, columns=[])
        assert build_table_doc(table).splitlines()[0] == "Table: legacy.payment_archive (empty)"

    def test_retriever_follows_schema_qualified_foreign_keys(self):
        doc = TableDoc("orders", "Table: orders\nForeign Keys:\n- (customer_id) -> sales.customers(id)", 1.0)
        assert referenced_tables([doc]) == {"sales.customers"}


class TestNames:
    def test_display_name(self):
        assert display_name("public", "orders") == "orders"
        assert display_name("sales", "orders") == "sales.orders"

    def test_quote_ident_escapes_quotes(self):
        assert quote_ident('we"ird') == '"we""ird"'

    @pytest.mark.parametrize(
        "pg_type, short",
        [
            ("character varying(40)", "varchar(40)"),
            ("character varying", "varchar"),
            ("character(20)", "char(20)"),
            ("timestamp without time zone", "timestamp"),
            ("timestamp(3) with time zone", "timestamptz(3)"),
            ("time without time zone", "time"),
            ("numeric(10,2)", "numeric(10,2)"),
            ("text[]", "text[]"),
        ],
    )
    def test_short_type(self, pg_type, short):
        assert short_type(pg_type) == short


class TestSensitiveColumns:
    @pytest.mark.parametrize(
        "name", ["password", "password_hash", "api_token", "email", "phone_number", "first_name", "lastname",
                 "username", "address", "postal_code", "date_of_birth", "card_number"],
    )
    def test_sensitive(self, name):
        assert is_sensitive_column(name)

    @pytest.mark.parametrize("name", ["name", "status", "country", "category", "rating", "title", "plan"])
    def test_not_sensitive(self, name):
        assert not is_sensitive_column(name)


class TestValuesFromStatistics:
    def test_count_of_distinct_values(self):
        assert values_from_statistics(["paid", "refunded"], 2, 10_000) == (("paid", "refunded"), 0)

    def test_fraction_of_rows(self):
        # -0.001 of 10,000 rows = 10 distinct values; 2 shown, 8 more.
        assert values_from_statistics(["paid", "refunded"], -0.001, 10_000) == (("paid", "refunded"), 8)

    def test_high_variety_columns_get_nothing(self):
        assert values_from_statistics(["Frank", "Mark"], -0.97, 59) == ((), 0)

    def test_long_values_are_free_text_not_categories(self):
        assert values_from_statistics(["x" * 41], 3, 100) == ((), 0)

    def test_at_most_shown_values(self):
        values = [f"v{i}" for i in range(20)]
        shown, more = values_from_statistics(values, 20, 1_000)
        assert len(shown) == SHOWN_VALUES and more == 20 - SHOWN_VALUES


class TestValuesFromRows:
    def test_most_common_first_then_alphabetical(self):
        assert values_from_rows(["b", "a", "b", None, "", "c"], unique_allowed=True) == (("b", "a", "c"), 0)

    def test_lookup_table_names_are_kept(self):
        assert values_from_rows(["Action", "Comedy", "Sci-Fi"], unique_allowed=True)[0] == ("Action", "Comedy", "Sci-Fi")

    def test_unique_values_in_people_tables_are_dropped(self):
        assert values_from_rows(["Mike", "Jon"], unique_allowed=False) == ((), 0)

    def test_repeated_values_in_people_tables_are_kept(self):
        assert values_from_rows(["Calgary", "Calgary", "Edmonton"], unique_allowed=False)[0] == ("Calgary", "Edmonton")

    def test_too_many_distinct_values(self):
        assert values_from_rows([f"v{i}" for i in range(30)], unique_allowed=True) == ((), 0)


# ── Real Postgres (opt-in): the Pagila eval database from backend/evals ───────

PAGILA_URL = os.environ.get("QM_TEST_PAGILA_URL")  # e.g. postgresql://postgres@localhost:55433/pagila


@pytest.mark.skipif(not PAGILA_URL, reason="set QM_TEST_PAGILA_URL (python -m evals.setup) to run")
class TestIntrospectPagila:
    @pytest.fixture(scope="class")
    def tables(self) -> dict[str, TableInfo]:
        original = settings.ALLOW_PRIVATE_DB_HOSTS
        settings.ALLOW_PRIVATE_DB_HOSTS = True
        try:
            schema = asyncio.run(introspect(normalize_url(PAGILA_URL)))
        finally:
            settings.ALLOW_PRIVATE_DB_HOSTS = original
        return {t.display_name: t for t in schema.tables}

    def test_partitions_fold_into_their_parent(self, tables):
        assert "payment" in tables
        assert not any(name.startswith("payment_p") for name in tables)
        payment_fks = {fk.referred_table for fk in tables["payment"].foreign_keys}
        assert payment_fks == {"customer", "rental", "staff"}

    def test_views_are_indexed(self, tables):
        assert tables["sales_by_store"].kind == "v"
        assert tables["rental_by_category"].kind == "m"

    def test_lookup_values_and_enums(self, tables):
        category_name = next(c for c in tables["category"].columns if c.name == "name")
        assert "Sci-Fi" in category_name.values or category_name.more_values
        rating = next(c for c in tables["film"].columns if c.name == "rating")
        assert rating.values == ("G", "PG", "PG-13", "R", "NC-17")

    def test_no_personal_values(self, tables):
        for table_name in ("staff", "customer"):
            assert all(not c.values for c in tables[table_name].columns), table_name
