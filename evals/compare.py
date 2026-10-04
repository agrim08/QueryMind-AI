"""Execution accuracy: does the generated query return the same answer as the gold query?

Two queries agree when their rows match as a multiset (row order is ignored) after values
are normalised (numbers to 2 decimals, padded text trimmed, dates as ISO strings).

Extra columns are allowed: a question asking for "emails of top customers" is answered
correctly by a query that also returns the amount spent. Every gold column must map to a
distinct generated column such that the rows still match.
"""
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

Verdict = Literal["exact", "extra_columns", "mismatch"]

Row = tuple[object, ...]


def normalize(value: object) -> object:
    """A comparable form of one database value."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), 2)
    if isinstance(value, str):
        return value.strip()  # char(n) columns come back blank-padded
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (list, tuple)):
        return tuple(normalize(v) for v in value)
    return str(value)


def _normalize_rows(rows: list[list]) -> list[Row]:
    return [tuple(normalize(v) for v in row) for row in rows]


def _column_mapping(gold: list[Row], pred: list[Row]) -> list[int] | None:
    """Generated column index for each gold column, or None if no mapping matches the rows."""
    gold_columns = list(zip(*gold))
    pred_columns = list(zip(*pred))
    # Only columns holding the same multiset of values can correspond.
    candidates = [
        [j for j, pc in enumerate(pred_columns) if Counter(pc) == Counter(gc)] for gc in gold_columns
    ]
    expected = Counter(gold)

    def assign(i: int, used: list[int]) -> list[int] | None:
        if i == len(gold_columns):
            return used if Counter(tuple(row[j] for j in used) for row in pred) == expected else None
        for j in candidates[i]:
            if j not in used and (found := assign(i + 1, [*used, j])) is not None:
                return found
        return None

    return assign(0, [])


def compare_results(gold_rows: list[list], pred_rows: list[list]) -> Verdict:
    """Compare a generated query's rows with the gold query's rows."""
    if len(gold_rows) != len(pred_rows):
        return "mismatch"
    gold, pred = _normalize_rows(gold_rows), _normalize_rows(pred_rows)
    if not gold:
        return "exact"
    gold_width, pred_width = len(gold[0]), len(pred[0])
    if pred_width < gold_width or _column_mapping(gold, pred) is None:
        return "mismatch"
    return "exact" if pred_width == gold_width else "extra_columns"
