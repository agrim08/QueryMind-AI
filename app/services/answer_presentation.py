"""How an answer is presented: chart choice, chart-ready data and a headline, all by rules (no AI).

The shape of the result decides what can be drawn; the question's intent (from the model's
reply) decides what should be:

  0 rows                              → "No rows matched" message
  1 row, only numbers                 → KPI figures                ("number")
  1 row, a label and a number         → one KPI with its label     ("which artist earned most?")
  1 row, anything else                → record card                ("details of order 1042")
  time column + numbers               → line chart                 ("trend")
  one label column + numbers, ≤ 25    → bar chart                  ("ranking", "breakdown", "comparison")
  anything else, or a list / record   → table

With several measures, the one the rows are ordered by (`ranked_by`) leads the chart and the
headline; each measure gets its own panel, because different measures never share an axis.

The headline is a template filled from the data, e.g. "Revenue went from 37.62 (Jan 2024) to
50.49 (Dec 2024), up 34%. Highest: 52.62 in Mar 2024." Charts get numbers as floats and labels
as display strings, so the frontend only draws.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from app.services.reply_format import SqlReply

ColumnKind = Literal["number", "time", "text", "other"]

MAX_BAR_ROWS = 25
MAX_BAR_MEASURES = 3
MAX_LINE_SERIES = 4
MAX_KPI_ITEMS = 4
MAX_RECORD_COLUMNS = 16

# Numeric columns that are really time: EXTRACT(year …) → 2023, "month" → 1..12.
_TIME_NAME = re.compile(
    r"(^|_)(year|month|quarter|week|day|date|period)(_|$)|^(extract|date_part|date_trunc)$", re.IGNORECASE
)
_TIME_TEXT = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$|^\d{4}-Q[1-4]$")
_GENERIC_NAME = re.compile(r"^(count|sum|avg|min|max|total|value|round|coalesce|\?column\?|extract|date_part)$", re.IGNORECASE)
_PERCENT_NAME = re.compile(r"percent|pct|percentage", re.IGNORECASE)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class Presentation:
    intent: str
    understood: str | None
    assumptions: list[str]
    alternatives: list[str]
    follow_ups: list[str]
    headline: str
    chart: dict = field(default_factory=lambda: {"kind": "table"})

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "understood": self.understood,
            "assumptions": self.assumptions,
            "alternatives": self.alternatives,
            "follow_ups": self.follow_ups,
            "headline": self.headline,
            "chart": self.chart,
        }


# ── Column types ──────────────────────────────────────────────────────────────

def _is_number(value: object) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def column_kind(name: str, values: list[object]) -> ColumnKind:
    """What a column holds, judged from its values (and its name for numeric time parts)."""
    present = [v for v in values if v is not None]
    if not present:
        return "other"
    if all(isinstance(v, (datetime, date)) for v in present):
        return "time"
    if all(_is_number(v) for v in present):
        if _TIME_NAME.search(name) and all(float(v).is_integer() and 1 <= float(v) <= 2200 for v in present):
            return "time"
        return "number"
    if all(isinstance(v, str) for v in present):
        return "time" if all(_TIME_TEXT.match(v) for v in present) else "text"
    if all(isinstance(v, (str, bool)) for v in present):
        return "text"
    return "other"


# ── Formatting ────────────────────────────────────────────────────────────────

def humanize(column: str) -> str:
    """'total_spent' → 'Total spent'."""
    words = re.sub(r"[_\s]+", " ", column).strip()
    return words[:1].upper() + words[1:] if words else column


def format_number(value: float, column: str = "") -> str:
    text = f"{int(value):,}" if float(value).is_integer() and abs(value) < 1e15 else f"{value:,.2f}"
    return text + ("%" if _PERCENT_NAME.search(column) else "")


def time_labels(name: str, values: list[object]) -> list[str]:
    """Readable labels for a time column: '2024', 'Mar 2024', '14 Mar 2024' or month names."""
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, (datetime, date)) for v in present):
        as_dates = [v.date() if isinstance(v, datetime) else v for v in present]
        has_time = any(isinstance(v, datetime) and (v.hour or v.minute) for v in present)
        if all(d.month == 1 and d.day == 1 for d in as_dates):
            fmt = "%Y"
        elif all(d.day == 1 for d in as_dates):
            fmt = "%b %Y"
        else:
            fmt = "%d %b %Y %H:%M" if has_time else "%d %b %Y"
        return [v.strftime(fmt) if v is not None else "" for v in values]
    if present and all(_is_number(v) for v in present):
        if re.search("month", name, re.IGNORECASE) and all(1 <= float(v) <= 12 for v in present):
            return [_MONTHS[int(v) - 1] if v is not None else "" for v in values]
        return [str(int(v)) if v is not None else "" for v in values]
    return ["" if v is None else str(v) for v in values]


def _label(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return time_labels("", [value])[0]
    return "—" if value is None else str(value)


def _subject(understood: str | None, column: str) -> str:
    if understood:
        return understood.rstrip(".?! ")
    return humanize(column)


# ── Charts ────────────────────────────────────────────────────────────────────

def _kpi(columns: list[str], row: list, numbers: list[int], understood: str | None) -> tuple[dict, str] | None:
    items = [
        {"label": humanize(columns[i]), "value": float(row[i]), "column": columns[i]}
        for i in numbers[:MAX_KPI_ITEMS]
        if row[i] is not None
    ]
    if not items:
        return None  # every value is NULL: the table says it best
    if len(items) == 1:
        items[0]["label"] = _subject(understood, items[0]["column"])
        headline = f"{items[0]['label']}: {format_number(items[0]['value'], items[0]['column'])}"
    else:
        headline = "; ".join(f"{it['label']}: {format_number(it['value'], it['column'])}" for it in items)
    return {"kind": "kpi", "items": items}, headline


def _labelled_kpi(
    columns: list[str], row: list, label_i: int, value_i: int, understood: str | None
) -> tuple[dict, str]:
    label, value = _label(row[label_i]), float(row[value_i])
    chart = {"kind": "kpi", "items": [{"label": label, "value": value, "column": columns[value_i]}]}
    headline = f"{_subject(understood, columns[value_i])}: {label} ({format_number(value, columns[value_i])})"
    return chart, headline


def _line(
    columns: list[str], rows: list[list], x: int, series_columns: list[int], split: int | None
) -> tuple[dict, str] | None:
    """A line chart over time column `x`; `split` is a text column to pivot into series."""
    ordered = sorted(rows, key=lambda r: (r[x] is None, r[x]))
    if split is None:
        labels = time_labels(columns[x], [r[x] for r in ordered])
        series = [
            {"name": humanize(columns[i]), "values": [None if r[i] is None else float(r[i]) for r in ordered]}
            for i in series_columns[:MAX_LINE_SERIES]
        ]
    else:
        keys = list(dict.fromkeys(r[x] for r in ordered))
        groups = list(dict.fromkeys(r[split] for r in ordered))
        if len(groups) > MAX_LINE_SERIES:
            return None
        value_i = series_columns[0]
        cells = {(r[x], r[split]): r[value_i] for r in ordered}
        labels = time_labels(columns[x], keys)
        series = [
            {
                "name": _label(g),
                "values": [None if cells.get((k, g)) is None else float(cells[(k, g)]) for k in keys],
            }
            for g in groups
        ]
    # One measure split by a category shares an axis; different measures (revenue and order
    # count) never do: they're drawn as separate panels.
    chart = {
        "kind": "line",
        "x_label": humanize(columns[x]),
        "labels": labels,
        "series": series,
        "shared_axis": split is not None or len(series) == 1,
    }
    return chart, _line_headline(labels, series)


def _line_headline(labels: list[str], series: list[dict]) -> str:
    if len(series) != 1:
        names = ", ".join(s["name"] for s in series)
        return f"{names} from {labels[0]} to {labels[-1]}."
    points = [(label, v) for label, v in zip(labels, series[0]["values"]) if v is not None]
    if len(points) < 2:
        return f"{series[0]['name']} for {labels[0]}."
    (first_label, first), (last_label, last) = points[0], points[-1]
    peak_label, peak = max(points, key=lambda p: p[1])
    name = series[0]["name"]
    if first:
        change = (last - first) / abs(first)
        direction = "up" if change > 0.005 else "down" if change < -0.005 else "flat"
        trend = f", {direction} {abs(change):.0%}" if direction != "flat" else ", about the same"
    else:
        trend = ""
    headline = f"{name} went from {format_number(first)} ({first_label}) to {format_number(last)} ({last_label}){trend}."
    if peak_label not in (first_label, last_label):
        headline += f" Highest: {format_number(peak)} in {peak_label}."
    return headline


def _monotonic(values: list[float], descending: bool) -> bool:
    pairs = list(zip(values, values[1:]))
    if descending:
        return all(a >= b for a, b in pairs) and any(a > b for a, b in pairs)
    return all(a <= b for a, b in pairs) and any(a < b for a, b in pairs)


def ranked_by(rows: list[list], numbers: list[int]) -> int:
    """The numeric column the rows are ordered by (a ranking's measure), else the first one.

    A query ranked by a measure returns that column sorted; other numbers in the same rows
    aren't. Descending (top N) is checked before ascending (bottom N).
    """
    for descending in (True, False):
        for i in numbers:
            values = [float(r[i]) for r in rows if r[i] is not None]
            if len(values) >= 2 and _monotonic(values, descending):
                return i
    return numbers[0]


def _measures(rows: list[list], numbers: list[int], limit: int) -> list[int]:
    """Numeric columns to chart, the ranking measure first."""
    primary = ranked_by(rows, numbers)
    return [primary, *(i for i in numbers if i != primary)][:limit]


def _bar(columns: list[str], rows: list[list], label_i: int, numbers: list[int], intent: str) -> tuple[dict, str]:
    """Bars per category; several measures become separate panels (never one shared axis)."""
    measures = _measures(rows, numbers, MAX_BAR_MEASURES)
    labels = [_label(r[label_i]) for r in rows]
    series = [
        {"name": humanize(columns[i]), "values": [None if r[i] is None else float(r[i]) for r in rows]}
        for i in measures
    ]
    chart = {"kind": "bar", "label_column": humanize(columns[label_i]), "labels": labels, "series": series}
    value_i, values = measures[0], series[0]["values"]
    present = [(label, v) for label, v in zip(labels, values) if v is not None]
    if not present:
        return chart, f"{len(rows)} results."
    column = columns[value_i]
    if len(measures) > 1:
        top_row = max((r for r in rows if r[value_i] is not None), key=lambda r: float(r[value_i]))
        others = ", ".join(
            f"{humanize(columns[i]).lower()}: {format_number(float(top_row[i]), columns[i])}"
            for i in measures[1:]
            if top_row[i] is not None
        )
        detail = f" ({others})" if others else ""
        return chart, (
            f"{_label(top_row[label_i])} leads on {humanize(column).lower()}: "
            f"{format_number(float(top_row[value_i]), column)}{detail}. {len(present)} shown."
        )
    if intent == "comparison" and len(present) == 2:
        (a, va), (b, vb) = sorted(present, key=lambda p: -p[1])
        diff = f" ({(va - vb) / abs(vb):.0%} higher)" if vb else ""
        return chart, f"{a} is ahead of {b}: {format_number(va, column)} vs {format_number(vb, column)}{diff}."
    top_label, top = max(present, key=lambda p: p[1])
    total = sum(v for _, v in present)
    share = ""
    if intent == "breakdown" and total > 0 and all(v >= 0 for _, v in present):
        share = f", {top / total:.0%} of the total"
    return chart, f"{top_label} is highest with {format_number(top, column)}{share} ({len(present)} shown)."


def _rows_headline(row_count: int, truncated: bool) -> str:
    if row_count == 0:
        return "No rows matched your question."
    if truncated:
        return f"Showing the first {row_count:,} rows. Add a filter or ask for a summary to see everything."
    return "1 row." if row_count == 1 else f"{row_count:,} rows."


def present(reply: SqlReply, columns: list[str], rows: list[list], truncated: bool) -> Presentation:
    """Choose the chart and headline for a successful query result."""
    presentation = Presentation(
        intent=reply.intent,
        understood=reply.understood,
        assumptions=list(reply.assumptions),
        alternatives=list(reply.alternatives),
        follow_ups=list(reply.follow_ups),
        headline=_rows_headline(len(rows), truncated),
    )
    if not rows:
        return presentation

    kinds = [column_kind(c, [r[i] for r in rows]) for i, c in enumerate(columns)]
    numbers = [i for i, k in enumerate(kinds) if k == "number"]
    times = [i for i, k in enumerate(kinds) if k == "time"]
    texts = [i for i, k in enumerate(kinds) if k == "text"]
    intent = reply.intent
    chosen: tuple[dict, str] | None = None

    if len(rows) == 1:
        row = rows[0]
        if numbers and len(numbers) == len(columns):
            chosen = _kpi(columns, row, numbers, reply.understood)
        elif len(numbers) == 1 and len(columns) == 2 and (texts or times) and intent != "record":
            chosen = _labelled_kpi(columns, row, (texts or times)[0], numbers[0], reply.understood)
        elif len(columns) <= MAX_RECORD_COLUMNS:
            chosen = {"kind": "record"}, reply.understood.rstrip(".") + "." if reply.understood else "1 matching record."
    elif intent not in ("list", "record") and not truncated:
        if times and [i for i in numbers if i != times[0]]:
            series_columns = [i for i in numbers if i != times[0]]
            split = texts[0] if len(texts) == 1 and len(series_columns) == 1 else None
            if not texts or split is not None:
                chosen = _line(columns, rows, times[0], series_columns, split)
        elif len(texts) == 1 and numbers and len(rows) <= MAX_BAR_ROWS:
            chosen = _bar(columns, rows, texts[0], numbers, intent)

    if chosen:
        presentation.chart, presentation.headline = chosen
    return presentation
