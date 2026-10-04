"""Unit tests for pure helpers: table documents, identifier quoting, usage window, entitlements."""
from datetime import datetime, timezone

import pytest

from app.api.deps import UNLIMITED, _build_entitlements
from app.services.schema_indexer import (
    SAMPLE_VALUE_MAX_CHARS,
    ColumnInfo,
    ForeignKeyInfo,
    TableInfo,
    _quote_ident,
    build_table_doc,
)
from app.services.usage import month_start_utc


class TestTableDoc:
    def test_document_format(self):
        table = TableInfo(
            name="orders",
            columns=[ColumnInfo("id", "UUID", False), ColumnInfo("total", "NUMERIC", True)],
            foreign_keys=[ForeignKeyInfo(["customer_id"], "customers", ["id"])],
            sample={"total": 42},
        )
        assert build_table_doc(table) == (
            "Table: orders\nColumns:\n- id (UUID) NOT NULL\n- total (NUMERIC) (e.g. 42)\n"
            "Foreign Keys:\n- (customer_id) -> customers(id)"
        )

    def test_long_sample_values_are_truncated(self):
        table = TableInfo(name="t", columns=[ColumnInfo("bio", "TEXT", True)], foreign_keys=[], sample={"bio": "x" * 500})
        assert f"(e.g. {'x' * SAMPLE_VALUE_MAX_CHARS})" in build_table_doc(table)

    def test_quote_ident_escapes_quotes(self):
        assert _quote_ident('we"ird') == '"we""ird"'


class TestUsageWindow:
    def test_month_start_is_first_of_month_utc(self):
        now = datetime(2026, 10, 17, 23, 59, tzinfo=timezone.utc)
        assert month_start_utc(now) == datetime(2026, 10, 1, tzinfo=timezone.utc)


class TestEntitlements:
    @pytest.mark.parametrize(
        ("fea", "connections", "queries", "designs", "csv", "pdf"),
        [
            ([], 1, 50, 1, False, False),
            (["pro_tier"], 5, UNLIMITED, 6, True, False),
            (["team_tier"], UNLIMITED, UNLIMITED, UNLIMITED, True, True),
            (["csv_export"], 1, 50, 1, True, False),
        ],
    )
    def test_plan_matrix(self, fea, connections, queries, designs, csv, pdf):
        plan = _build_entitlements({"fea": fea})
        assert (plan.max_connections, plan.max_queries_pm, plan.max_designs_pm) == (connections, queries, designs)
        assert (plan.csv_export, plan.pdf_export) == (csv, pdf)

    def test_team_implies_pro(self):
        assert _build_entitlements({"fea": ["team_tier"]}).is_pro
