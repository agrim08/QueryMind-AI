"""The model's reply format, and how it's split while streaming.

Plain text, not JSON, so the SQL can stream to the browser token by token. A reply is a few
header comment lines, then exactly one body:

    -- Intent: trend
    -- Understood: Monthly revenue in 2024
    -- Assumption: revenue means the sum of invoice totals        (0–2 lines)
    <body>

    body = SQL statement, optionally followed by
               -- Instead: <the question with another reasonable reading>   (0–2)
               -- Follow-up: <a natural next question>                      (0–3)
         | -- Clarify: <one question>   then 2–4 lines  -- Option: <a reading>
         | -- Answer: <plain text>      (questions about the database itself; may repeat)
         | -- Not allowed: <what they asked to change>
         | -- Cannot answer: <reason>

Only comment lines *before* or *after* the statement are metadata. They're never executed:
`SqlReply.sql` is the statement alone, and it is exactly what gets validated and run.
"""
import re
from dataclasses import dataclass
from typing import Literal

Intent = Literal["number", "trend", "ranking", "breakdown", "comparison", "list", "record", "schema", "other"]
INTENTS: tuple[str, ...] = Intent.__args__  # type: ignore[attr-defined]

MAX_ASSUMPTIONS = 2
MAX_ALTERNATIVES = 2
MAX_FOLLOW_UPS = 3
MAX_OPTIONS = 4
MAX_LINE_CHARS = 300

# Model replies sometimes arrive wrapped in a Markdown fence despite the prompt.
_FENCED = re.compile(r"```[\w-]*[ \t]*\n(.*?)```", re.DOTALL)
_TAGGED = re.compile(r"^--\s*([a-z][a-z -]*?)\s*:\s*(.*)$", re.IGNORECASE)
# "-- Cannot answer the question because…" without a colon is still a decline.
_CANNOT_ANSWER_LOOSE = re.compile(r"^--\s*cannot answer\s*:?\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Clarification:
    question: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class SqlReply:
    """The model's reply, split by `parse_reply`. At most one of `sql`, `clarify`, `answer`,
    `not_allowed`, `cannot_answer` is set (an empty reply has none)."""

    sql: str = ""  # the statement to validate and execute
    intent: Intent = "other"
    understood: str | None = None
    assumptions: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    follow_ups: tuple[str, ...] = ()
    clarify: Clarification | None = None
    answer: str | None = None  # plain-English answer about the database itself
    not_allowed: str | None = None  # a request to change data
    cannot_answer: str | None = None  # the model's reason, when the data can't answer it


def _clean(text: str) -> str:
    return " ".join(text.split())[:MAX_LINE_CHARS]


def _split_lines(text: str) -> tuple[list[str], list[str], list[str]]:
    """(leading comment lines, statement lines, trailing comment lines)."""
    lines = [line for line in text.splitlines() if line.strip()]
    start = 0
    while start < len(lines) and lines[start].lstrip().startswith("--"):
        start += 1
    end = len(lines)
    while end > start and lines[end - 1].lstrip().startswith("--"):
        end -= 1
    return lines[:start], lines[start:end], lines[end:]


def parse_reply(text: str) -> SqlReply:
    """Split a complete reply into its parts (see the module docstring)."""
    text = text.strip()
    if fenced := _FENCED.search(text):
        text = fenced.group(1).strip()
    leading, statement, trailing = _split_lines(text)

    fields: dict[str, list[str]] = {}
    for line in leading + trailing:
        line = line.strip()
        if match := _TAGGED.match(line):
            fields.setdefault(match.group(1).lower(), []).append(_clean(match.group(2)))
        elif match := _CANNOT_ANSWER_LOOSE.match(line):
            fields.setdefault("cannot answer", []).append(_clean(match.group(1)))

    def first(key: str) -> str | None:
        return fields.get(key, [None])[0]

    intent = (first("intent") or "other").lower()
    common = {
        "intent": intent if intent in INTENTS else "other",
        "understood": first("understood"),
        "assumptions": tuple(fields.get("assumption", [])[:MAX_ASSUMPTIONS]),
    }
    if "cannot answer" in fields:
        return SqlReply(**common, cannot_answer=first("cannot answer") or "")
    if "not allowed" in fields:
        return SqlReply(**common, not_allowed=first("not allowed") or "")
    if "clarify" in fields and not statement:
        options = tuple(fields.get("option", [])[:MAX_OPTIONS])
        if options:
            return SqlReply(**common, clarify=Clarification(first("clarify") or "", options))
    if "answer" in fields and not statement:
        return SqlReply(**{**common, "intent": "schema"}, answer=" ".join(fields["answer"]))
    return SqlReply(
        **common,
        sql="\n".join(statement).strip(),
        alternatives=tuple(fields.get("instead", [])[:MAX_ALTERNATIVES]),
        follow_ups=tuple(fields.get("follow-up", [])[:MAX_FOLLOW_UPS]),
    )


class SqlStreamFilter:
    """Splits the streamed reply so only the SQL statement reaches the browser live.

    Statement text is passed through as soon as it's known to be SQL, so it still streams
    token by token. A line is classified after its first two non-space characters:

    - before the statement, `--` lines are the header (metadata): dropped;
    - after the statement has started, a `--` line may be a comment inside the statement or
      the trailing suggestions; it's held back and released only if more SQL follows.
    """

    def __init__(self) -> None:
        self._line = ""  # the current line, until it's classified
        self._line_is_sql: bool | None = None
        self._in_statement = False
        self._held: list[str] = []  # comment lines seen after the statement started

    def feed(self, chunk: str) -> str:
        """The part of `chunk` that is SQL and can be shown now."""
        out: list[str] = []
        for char in chunk:
            if self._line_is_sql:
                out.append(char)
                if char == "\n":
                    self._line, self._line_is_sql = "", None
                continue
            self._line += char
            if char == "\n":
                if self._line_is_sql is False and self._in_statement:
                    self._held.append(self._line)
                self._line, self._line_is_sql = "", None  # header and blank lines are dropped
                continue
            if self._line_is_sql is False:
                continue
            head = self._line.lstrip()
            if len(head) >= 2 or (head and head[0] != "-"):
                if head.startswith("--"):
                    self._line_is_sql = False
                else:
                    # More statement: comment lines held since the last SQL line were inside it.
                    out.extend(self._held)
                    self._held.clear()
                    self._line_is_sql = self._in_statement = True
                    out.append(self._line)
        return "".join(out)

    def finish(self) -> str:
        """Flush a final unterminated line that turned out to be SQL (trailing comments are dropped)."""
        head = self._line.lstrip()
        if self._line_is_sql is None and head and not head.startswith("--"):
            return "".join(self._held) + self._line
        return ""
