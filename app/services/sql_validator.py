"""SQL Validator — keyword blocklist + sqlparse structural check.

Hard rules:
- Only SELECT statements are allowed.
- Blocklist covers all mutating / DDL keywords, SELECT ... INTO, row locks,
  and side-effecting functions (pg_terminate_backend, pg_sleep, dblink, ...).
- This is one layer of several: the executor also runs inside a READ ONLY transaction.
- Table names in the query must exist in the provided schema.
"""
import re
from dataclasses import dataclass

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import Keyword, DDL, DML

# Mutating / dangerous keywords — block any query containing these
_BLOCKLIST: frozenset[str] = frozenset(
    {
        "DROP",
        "DELETE",
        "INSERT",
        "UPDATE",
        "ALTER",
        "TRUNCATE",
        "CREATE",
        "REPLACE",
        "MERGE",
        "GRANT",
        "REVOKE",
        "EXEC",
        "EXECUTE",
        "CALL",
        "COPY",
        "VACUUM",
        "ANALYZE",
        "INTO",  # SELECT ... INTO creates a table
    }
)

# Row-locking clauses (FOR UPDATE is already caught by the UPDATE keyword).
_ROW_LOCK_PATTERN = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+UPDATE|KEY\s+SHARE|SHARE)\b")

# Functions with side effects that a SELECT can call. The executor's READ ONLY
# transaction stops writes, but not these (killing sessions, sleeping, reading
# server files, opening connections elsewhere, or running SQL hidden in a string).
_FORBIDDEN_FUNCTION_PATTERN = re.compile(
    r"\b("
    r"PG_SLEEP\w*|PG_TERMINATE_BACKEND|PG_CANCEL_BACKEND|PG_RELOAD_CONF|"
    r"PG_ROTATE_LOGFILE|PG_SWITCH_WAL|PG_CREATE_\w+|PG_DROP_\w+|PG_NOTIFY|"
    r"PG_(?:TRY_)?ADVISORY\w*|PG_READ_\w+|PG_LS_\w+|PG_STAT_FILE|"
    r"LO_\w+|DBLINK\w*|SET_CONFIG|NEXTVAL|SETVAL|QUERY_TO_\w+|CURSOR_TO_\w+"
    r")\s*\("
)


@dataclass
class ValidationResult:
    is_valid: bool
    error: str | None = None


def _extract_keywords(statement: Statement) -> list[str]:
    """Return all keyword token values (upper-cased) from a parsed statement."""
    keywords: list[str] = []
    for token in statement.flatten():
        if token.ttype in (Keyword, DDL, DML):
            keywords.append(token.normalized.upper())
    return keywords


def validate_sql(sql: str, known_tables: list[str] | None = None) -> ValidationResult:
    """
    Validate a SQL string.

    Args:
        sql: The SQL string to validate.
        known_tables: Optional list of table names that exist in the schema.
                      If provided, referenced tables are checked against this list.

    Returns:
        ValidationResult with is_valid=True or an error message.
    """
    sql = sql.strip()
    if not sql:
        return ValidationResult(is_valid=False, error="Empty SQL query")

    # If the model couldn't answer with the available schema, surface it clearly
    if sql.startswith("--"):
        first_line = sql.splitlines()[0]
        return ValidationResult(
            is_valid=False,
            error=f"The AI could not generate a query for that question: {first_line.lstrip('- ').strip()}",
        )

    # --- 1. Blocklist check (fast regex pass before parsing) ---
    upper_sql = sql.upper()
    for keyword in _BLOCKLIST:
        # Use word-boundary regex to avoid false positives (e.g. "DELETED" column)
        if re.search(rf"\b{keyword}\b", upper_sql):
            return ValidationResult(
                is_valid=False,
                error=f"Forbidden keyword detected: {keyword}. Only SELECT queries are allowed.",
            )

    if _ROW_LOCK_PATTERN.search(upper_sql):
        return ValidationResult(
            is_valid=False,
            error="Row-locking clauses (FOR SHARE / FOR UPDATE) are not allowed.",
        )

    function_match = _FORBIDDEN_FUNCTION_PATTERN.search(upper_sql)
    if function_match:
        return ValidationResult(
            is_valid=False,
            error=(
                f"Forbidden function detected: {function_match.group(1).lower()}. "
                "Only read-only queries are allowed."
            ),
        )

    # --- 2. Parse and verify it's a single SELECT statement ---
    parsed = sqlparse.parse(sql)
    if not parsed or len(parsed) == 0:
        return ValidationResult(is_valid=False, error="Could not parse SQL")

    if len(parsed) > 1:
        return ValidationResult(
            is_valid=False, error="Multiple statements are not allowed"
        )

    statement = parsed[0]
    keywords = _extract_keywords(statement)

    if not keywords or keywords[0] != "SELECT":
        return ValidationResult(
            is_valid=False,
            error=f"Only SELECT statements are allowed. Got: {keywords[0] if keywords else 'unknown'}",
        )

    # --- 3. Optional: verify referenced tables exist in schema ---
    if known_tables:
        known_lower = {t.lower() for t in known_tables}
        # Extract identifiers that look like table names (simple heuristic)
        from_pattern = re.compile(
            r"\bFROM\s+([\w\".]+)|\bJOIN\s+([\w\".]+)", re.IGNORECASE
        )
        referenced = set()
        for match in from_pattern.finditer(sql):
            table = (match.group(1) or match.group(2)).strip('"').lower()
            # Strip schema prefix if present (e.g. public.users -> users)
            table = table.split(".")[-1]
            referenced.add(table)

        unknown = referenced - known_lower
        if unknown:
            return ValidationResult(
                is_valid=False,
                error=f"Query references unknown table(s): {', '.join(sorted(unknown))}",
            )

    return ValidationResult(is_valid=True)
