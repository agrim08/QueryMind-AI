"""Unit tests for answer presentation: chart choice, chart data and headlines (no AI)."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.services.answer_presentation import column_kind, format_number, humanize, present, time_labels
from app.services.reply_format import SqlReply
from app.services.schema_answers import describe_tables, is_table_listing


def _present(intent, columns, rows, understood=None, truncated=False):
    return present(SqlReply(sql="SELECT 1", intent=intent, understood=understood), columns, rows, truncated)


class TestColumnKind:
    def test_basic_kinds(self):
        assert column_kind("total", [Decimal("1.5"), 2, None]) == "number"
        assert column_kind("created_at", [datetime(2024, 1, 1), None]) == "time"
        assert column_kind("country", ["USA", "Canada"]) == "text"
        assert column_kind("active", [True, False]) == "text"
        assert column_kind("x", [None, None]) == "other"

    @pytest.mark.parametrize("name", ["year", "order_year", "month", "extract", "date_part"])
    def test_numeric_time_parts(self, name):
        assert column_kind(name, [Decimal("2023"), Decimal("2024")]) == "time"

    @pytest.mark.parametrize("name", ["days_to_ship", "yearly_revenue", "total"])
    def test_numbers_that_are_not_time(self, name):
        assert column_kind(name, [3, 12]) == "number"

    def test_period_strings(self):
        assert column_kind("period", ["2024-01", "2024-02"]) == "time"
        assert column_kind("code", ["2024-AB"]) == "text"


class TestFormatting:
    def test_numbers(self):
        assert format_number(1234567.0) == "1,234,567"
        assert format_number(1234.5) == "1,234.50"
        assert format_number(37.04, "pct_rock") == "37.04%"

    def test_humanize(self):
        assert humanize("total_spent") == "Total spent"

    def test_time_labels(self):
        assert time_labels("m", [datetime(2024, 1, 1), datetime(2024, 2, 1)]) == ["Jan 2024", "Feb 2024"]
        assert time_labels("y", [date(2023, 1, 1), date(2024, 1, 1)]) == ["2023", "2024"]
        assert time_labels("d", [date(2024, 3, 14)]) == ["14 Mar 2024"]
        assert time_labels("month", [Decimal(1), Decimal(12)]) == ["Jan", "Dec"]
        assert time_labels("year", [Decimal(2021)]) == ["2021"]


class TestCharts:
    def test_empty_result(self):
        p = _present("list", ["email"], [])
        assert p.chart == {"kind": "table"} and p.headline == "No rows matched your question."

    def test_single_number_is_a_kpi_named_by_the_understood_question(self):
        p = _present("number", ["sum"], [[Decimal("469.58")]], understood="Total sales in 2023")
        assert p.chart["kind"] == "kpi"
        assert p.chart["items"] == [{"label": "Total sales in 2023", "value": 469.58, "column": "sum"}]
        assert p.headline == "Total sales in 2023: 469.58"

    def test_several_numbers_are_several_kpis(self):
        p = _present("number", ["orders", "revenue"], [[412, Decimal("2328.6")]])
        assert [i["label"] for i in p.chart["items"]] == ["Orders", "Revenue"]
        assert p.headline == "Orders: 412; Revenue: 2,328.60"

    def test_label_and_value_in_one_row_is_a_labelled_kpi(self):
        p = _present("ranking", ["artist", "revenue"], [["Iron Maiden", Decimal("138.6")]],
                     understood="Artist with the most revenue")
        assert p.chart["items"][0]["label"] == "Iron Maiden"
        assert p.headline == "Artist with the most revenue: Iron Maiden (138.60)"

    def test_one_detailed_row_is_a_record(self):
        p = _present("record", ["email", "city", "country"], [["a@b.c", "Paris", "France"]],
                     understood="Details of customer 42")
        assert p.chart == {"kind": "record"} and p.headline == "Details of customer 42."

    def test_trend_is_a_sorted_line_with_a_change_headline(self):
        rows = [[datetime(2024, 3, 1), 52.62], [datetime(2024, 1, 1), 37.62], [datetime(2024, 2, 1), 40.0],
                [datetime(2024, 4, 1), 50.49]]
        p = _present("trend", ["month", "revenue"], rows)
        assert p.chart["kind"] == "line"
        assert p.chart["labels"] == ["Jan 2024", "Feb 2024", "Mar 2024", "Apr 2024"]
        assert p.chart["series"] == [{"name": "Revenue", "values": [37.62, 40.0, 52.62, 50.49]}]
        assert p.headline == (
            "Revenue went from 37.62 (Jan 2024) to 50.49 (Apr 2024), up 34%. Highest: 52.62 in Mar 2024."
        )

    def test_trend_by_category_pivots_into_series(self):
        rows = [[2023, "USA", 10], [2023, "Canada", 5], [2024, "USA", 12], [2024, "Canada", 7]]
        p = _present("trend", ["year", "country", "orders"], rows)
        assert p.chart["labels"] == ["2023", "2024"]
        assert p.chart["series"] == [{"name": "USA", "values": [10.0, 12.0]}, {"name": "Canada", "values": [5.0, 7.0]}]
        assert p.chart["shared_axis"] is True  # one measure split by country: one axis is right

    def test_ranking_is_a_bar_with_the_leader_in_the_headline(self):
        rows = [["Brazil", 5], ["USA", 13], ["Canada", 8]]
        p = _present("ranking", ["country", "customers"], rows)
        assert p.chart == {
            "kind": "bar", "label_column": "Country",
            "labels": ["Brazil", "USA", "Canada"],
            "series": [{"name": "Customers", "values": [5.0, 13.0, 8.0]}],
        }
        assert p.headline == "USA is highest with 13 (3 shown)."

    def test_several_measures_rank_by_the_one_the_rows_are_ordered_by(self):
        # Regression (2026-10-04): "linked users vs questions asked for the top 3 projects" was
        # charted by linked users, the first numeric column, so "cpp is highest with 2".
        rows = [["AI-Madness", 1, 15], ["TypeScript", 1, 4], ["cpp", 2, 2]]
        p = _present("comparison", ["project_name", "linked_users", "questions_asked"], rows)
        assert p.chart["labels"] == ["AI-Madness", "TypeScript", "cpp"]
        assert p.chart["series"] == [
            {"name": "Questions asked", "values": [15.0, 4.0, 2.0]},
            {"name": "Linked users", "values": [1.0, 1.0, 2.0]},
        ]
        assert p.headline == "AI-Madness leads on questions asked: 15 (linked users: 1). 3 shown."

    def test_ascending_order_counts_as_ranked_too(self):
        rows = [["a", 9, 1], ["b", 3, 2], ["c", 7, 5]]
        p = _present("ranking", ["name", "score", "rank_cost"], rows)
        assert p.chart["series"][0]["name"] == "Rank cost"

    def test_unordered_measures_keep_their_order(self):
        rows = [["a", 9, 1], ["b", 3, 7], ["c", 7, 5]]
        p = _present("breakdown", ["name", "x", "y"], rows)
        assert [s["name"] for s in p.chart["series"]] == ["X", "Y"]

    def test_several_measures_over_time_never_share_an_axis(self):
        rows = [[2023, 1000.0, 12], [2024, 1500.0, 15]]
        p = _present("trend", ["year", "revenue", "orders"], rows)
        assert p.chart["kind"] == "line" and p.chart["shared_axis"] is False
        assert [s["name"] for s in p.chart["series"]] == ["Revenue", "Orders"]

    def test_breakdown_mentions_the_share(self):
        p = _present("breakdown", ["status", "orders"], [["paid", 75], ["refunded", 25]])
        assert p.headline == "paid is highest with 75, 75% of the total (2 shown)."

    def test_comparison_of_two(self):
        p = _present("comparison", ["store", "revenue"], [["Store 1", 100], ["Store 2", 125]])
        assert p.headline == "Store 2 is ahead of Store 1: 125 vs 100 (25% higher)."

    def test_lists_stay_tables(self):
        p = _present("list", ["country", "customers"], [["USA", 13], ["Canada", 8]])
        assert p.chart == {"kind": "table"} and p.headline == "2 rows."

    def test_too_many_categories_stay_a_table(self):
        p = _present("breakdown", ["city", "n"], [[f"c{i}", i] for i in range(40)])
        assert p.chart == {"kind": "table"}

    def test_truncated_results_say_so(self):
        p = _present("list", ["email"], [["x"]] * 500, truncated=True)
        assert p.headline.startswith("Showing the first 500 rows.")

    def test_suggestions_are_passed_through(self):
        reply = SqlReply(sql="SELECT 1", alternatives=("by orders",), follow_ups=("by month?",), assumptions=("a",))
        p = present(reply, ["n"], [[1]], False)
        assert (p.alternatives, p.follow_ups, p.assumptions) == (["by orders"], ["by month?"], ["a"])


class TestSchemaAnswers:
    @pytest.mark.parametrize(
        "question",
        ["What tables do I have?", "which tables are there", "list all tables", "show me the schema",
         "What's in my database?", "what can I ask"],
    )
    def test_table_listing_questions(self, question):
        assert is_table_listing(question)

    @pytest.mark.parametrize(
        "question", ["What tables have customer emails?", "How many customers do we have?", "list customers in Canada"]
    )
    def test_other_questions_go_to_the_model(self, question):
        # "What tables have customer emails?" needs the model: it's about content, not the list.
        assert not is_table_listing(question)

    def test_describe_tables(self):
        assert describe_tables(["invoice", "Album"]).startswith("Your database has 2 tables and views: Album, invoice.")
