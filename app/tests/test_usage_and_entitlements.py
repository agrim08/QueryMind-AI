"""Unit tests for pure helpers: usage window and entitlements (table documents: test_schema_introspection.py)."""
from datetime import datetime, timezone

import pytest

from app.api.deps import UNLIMITED, _build_entitlements
from app.services.usage import month_start_utc


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
