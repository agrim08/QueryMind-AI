"""Unit tests for the eval scorer (evals/compare.py)."""
from datetime import date
from decimal import Decimal

from evals.compare import compare_results, normalize


class TestNormalize:
    def test_numbers_compare_across_types(self):
        assert normalize(Decimal("3.252295")) == normalize(3.25) == 3.25
        assert normalize(2021) == normalize(Decimal("2021")) == normalize(2021.0)

    def test_blank_padded_text_is_trimmed(self):
        assert normalize("English              ") == "English"

    def test_dates_and_arrays(self):
        assert normalize(date(2024, 1, 1)) == "2024-01-01"
        assert normalize(["Trailers", "Deleted Scenes"]) == ("Trailers", "Deleted Scenes")


class TestCompareResults:
    def test_identical_rows_in_any_order(self):
        assert compare_results([["a", 1], ["b", 2]], [["b", 2], ["a", 1]]) == "exact"

    def test_columns_in_a_different_order(self):
        assert compare_results([["a", 1], ["b", 2]], [[2, "b"], [1, "a"]]) == "exact"

    def test_extra_columns_are_allowed(self):
        gold = [["karl@example.com"], ["ellie@example.com"]]
        pred = [[1, "karl@example.com", Decimal("221.55")], [2, "ellie@example.com", Decimal("216.54")]]
        assert compare_results(gold, pred) == "extra_columns"

    def test_missing_column_is_a_mismatch(self):
        assert compare_results([["a", 1]], [["a"]]) == "mismatch"

    def test_different_row_count_is_a_mismatch(self):
        assert compare_results([["a"]], [["a"], ["b"]]) == "mismatch"

    def test_duplicates_count(self):
        assert compare_results([["a"], ["a"]], [["a"], ["b"]]) == "mismatch"
        assert compare_results([["a"], ["a"]], [["a"], ["a"]]) == "exact"

    def test_columns_must_line_up_row_by_row(self):
        # Each column holds the same values, but paired differently.
        gold = [["a", 1], ["b", 2]]
        pred = [["a", 2], ["b", 1]]
        assert compare_results(gold, pred) == "mismatch"

    def test_column_mapping_backtracks_over_identical_columns(self):
        # Gold column 2 can only map to pred column 2 once column 0 is taken.
        gold = [[1, 1, "x"], [2, 2, "y"]]
        pred = [[1, "x", 1], [2, "y", 2]]
        assert compare_results(gold, pred) == "exact"

    def test_rounding_tolerates_float_noise(self):
        assert compare_results([[Decimal("4.2150476795931341")]], [[4.215047679]]) == "exact"

    def test_empty_results_agree(self):
        assert compare_results([], []) == "exact"
