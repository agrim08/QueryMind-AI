"""Answer snapshots stay within their row and size bounds and keep what was shown."""
import json
from datetime import date
from decimal import Decimal

from app.services.answer_snapshot import MAX_BYTES, MAX_ROWS, build
from app.services.query_executor import QueryResult

ANSWER = {"headline": "USA is highest with 13.", "chart": {"kind": "bar", "labels": ["USA"], "series": []}}


def _result(rows: list[list], truncated: bool = False) -> QueryResult:
    return QueryResult(columns=["name", "total"], rows=rows, exec_time_ms=5, row_count=len(rows), truncated=truncated)


def test_small_answers_are_kept_whole():
    snapshot = build(ANSWER, _result([["USA", 13], ["Canada", 8]]))
    assert snapshot == {
        "answer": ANSWER,
        "columns": ["name", "total"],
        "rows": [["USA", 13], ["Canada", 8]],
        "row_count": 2,
        "truncated": False,
    }


def test_rows_are_capped_but_the_real_row_count_is_kept():
    snapshot = build(ANSWER, _result([[f"c{i}", i] for i in range(500)], truncated=True))
    assert len(snapshot["rows"]) == MAX_ROWS
    assert snapshot["row_count"] == 500
    assert snapshot["truncated"] is True


def test_values_are_stored_as_the_browser_saw_them():
    snapshot = build(ANSWER, _result([[date(2026, 10, 5), Decimal("12.50")]]))
    assert snapshot["rows"] == [["2026-10-05", "12.50"]]


def test_large_answers_drop_rows_before_the_headline():
    wide = "x" * 2_000
    snapshot = build(ANSWER, _result([[wide, i] for i in range(MAX_ROWS)]))
    assert len(json.dumps(snapshot).encode()) <= MAX_BYTES
    assert 0 < len(snapshot["rows"]) < MAX_ROWS
    assert snapshot["answer"]["headline"] == ANSWER["headline"]


def test_a_chart_too_large_to_keep_falls_back_to_the_table():
    huge_chart = {**ANSWER, "chart": {"kind": "bar", "labels": ["x" * 40_000], "series": []}}
    snapshot = build(huge_chart, _result([["USA", 13]]))
    assert snapshot["answer"]["chart"] == {"kind": "table"}
    assert snapshot["answer"]["headline"] == ANSWER["headline"]
    assert len(json.dumps(snapshot).encode()) <= MAX_BYTES

