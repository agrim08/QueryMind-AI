"""Schema introspection: what a target database contains, read from the Postgres catalogs.

Everything runs in one read-only transaction with a statement timeout, using a fixed
handful of catalog queries whatever the schema size (no per-table round trips), plus at
most one bounded read per small table:

- Tables, partitioned tables, views, materialized views and foreign tables in every schema
  the connecting role can read. Skipped: system and platform-managed schemas, objects owned
  by extensions, and individual partitions (their parent stands for them).
- Columns with types, NOT NULL, primary keys, enum labels and the owner's comments.
- Foreign keys, with schema-qualified targets outside `public`.
- Approximate row counts from `pg_class.reltuples` (no COUNT(*) scans); a never-analysed
  table with nothing on disk is known to be empty.
- Example values only for low-variety text columns (status, country, category name...):
  from Postgres's own statistics (`pg_stats`) for analysed tables, or by reading at most
  SMALL_TABLE_ROWS rows of small tables, which autovacuum never analyses. Columns and tables
  that look like personal data never contribute values.
"""
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import ARRAY, BigInteger, Text, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.ai_config import MAX_INDEXED_TABLES
from app.services.schema_store import display_name
from app.services.target_db import open_target_engine

logger = logging.getLogger(__name__)

INTROSPECTION_TIMEOUT_MS = 15_000
# Tables with at most this many rows (or never analysed) are read directly for example values.
SMALL_TABLE_ROWS = 200
# A text column gets example values only if it has at most this many distinct values.
MAX_DISTINCT_VALUES = 25
SHOWN_VALUES = 12
VALUE_MAX_CHARS = 40

# Schemas managed by Postgres itself or by hosting platforms (Supabase, TimescaleDB, PostGIS
# topology, pg_cron...). Their tables are infrastructure, not the user's data model.
PLATFORM_SCHEMAS = (
    "information_schema", "auth", "storage", "realtime", "_realtime", "supabase_functions",
    "supabase_migrations", "extensions", "graphql", "graphql_public", "vault", "pgsodium",
    "pgsodium_masks", "net", "cron", "pgbouncer", "_analytics", "topology", "tiger", "tiger_data",
    "_timescaledb_catalog", "_timescaledb_internal", "_timescaledb_cache", "_timescaledb_config",
    "timescaledb_information", "timescaledb_experimental",
)

# Never show values from columns like these, or unique values from tables about people.
_SENSITIVE_COLUMN = re.compile(
    r"pass|secret|token|hash|salt|email|phone|mobile|ssn|iban|card|address|street|postal|zip"
    r"|birth|dob|(^|_)(first|last|full|middle|user|display)_?name$",
    re.IGNORECASE,
)
_PEOPLE_TABLE = re.compile(
    r"user|customer|client|staff|employee|member|account|person|people|contact|patient|student"
    r"|author|profile|admin|guest|subscriber",
    re.IGNORECASE,
)

_TYPE_ABBREVIATIONS = (
    (re.compile(r"character varying"), "varchar"),
    (re.compile(r"^character\b"), "char"),
    (re.compile(r"timestamp(\(\d+\))? without time zone"), r"timestamp\1"),
    (re.compile(r"timestamp(\(\d+\))? with time zone"), r"timestamptz\1"),
    (re.compile(r"time(\(\d+\))? without time zone"), r"time\1"),
)


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    primary_key: bool = False
    comment: str | None = None
    values: tuple[str, ...] = ()  # example values, most common first
    more_values: int = 0  # distinct values not shown
    is_text: bool = False


@dataclass(frozen=True)
class ForeignKeyInfo:
    columns: list[str]
    referred_table: str  # display name
    referred_columns: list[str]


@dataclass
class TableInfo:
    schema: str
    name: str
    kind: str  # pg_class.relkind: r table, p partitioned, v view, m materialized view, f foreign
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    row_estimate: int = -1  # -1 when unknown (never analysed, or a view)
    comment: str | None = None

    @property
    def display_name(self) -> str:
        return display_name(self.schema, self.name)


@dataclass(frozen=True)
class IntrospectedSchema:
    tables: list[TableInfo]
    truncated: bool  # more than MAX_INDEXED_TABLES relations exist; the rest were skipped


_RELATIONS = text("""
SELECT c.oid::bigint AS oid, n.nspname AS schema_name, c.relname AS name, c.relkind::text AS kind,
       (CASE WHEN c.relkind = 'p' THEN (
                SELECT coalesce(sum(greatest(ch.reltuples, 0)), -1)
                FROM pg_inherits i JOIN pg_class ch ON ch.oid = i.inhrelid
                WHERE i.inhparent = c.oid)
             -- Never analysed and nothing on disk: known to be empty (pg_relation_size reads no rows).
             WHEN c.reltuples < 0 AND c.relkind IN ('r', 'm') AND pg_relation_size(c.oid) = 0 THEN 0
             ELSE c.reltuples END)::bigint AS row_estimate,
       obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND NOT c.relispartition
  AND left(n.nspname, 3) <> 'pg_'
  AND n.nspname <> ALL(:excluded_schemas)
  AND NOT EXISTS (SELECT 1 FROM pg_depend d
                  WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY n.nspname <> 'public', c.relkind IN ('v', 'f'), row_estimate DESC, n.nspname, c.relname
LIMIT :limit
""").bindparams(bindparam("excluded_schemas", type_=ARRAY(Text)))

_COLUMNS = text("""
SELECT a.attrelid::bigint AS oid, a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
       NOT a.attnotnull AS nullable, col_description(a.attrelid, a.attnum) AS comment,
       t.typcategory = 'S' AS is_text,
       (SELECT array_agg(e.enumlabel::text ORDER BY e.enumsortorder)
        FROM pg_enum e WHERE e.enumtypid = a.atttypid) AS enum_labels
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
WHERE a.attrelid::bigint = ANY(:oids) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
""").bindparams(bindparam("oids", type_=ARRAY(BigInteger)))

# Keys of each relation. A partitioned table also takes its partitions' keys, because older
# schemas define foreign keys on every partition rather than on the parent.
_CONSTRAINTS = text("""
WITH owners AS (
    SELECT c.oid AS relid, c.oid AS owner FROM pg_class c WHERE c.oid::bigint = ANY(:oids)
    UNION ALL
    SELECT i.inhrelid, i.inhparent FROM pg_inherits i WHERE i.inhparent::bigint = ANY(:oids)
), keys AS (
    SELECT o.owner::bigint AS oid, con.contype::text AS kind, con.conname,
           rn.nspname::text AS ref_schema, rc.relname::text AS ref_name,
           ARRAY(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
                 JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                 ORDER BY k.ord) AS columns,
           ARRAY(SELECT a.attname::text FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
                 JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
                 ORDER BY k.ord) AS ref_columns
    FROM pg_constraint con
    JOIN owners o ON o.relid = con.conrelid
    LEFT JOIN pg_class rc ON rc.oid = con.confrelid
    LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace
    WHERE con.contype IN ('p', 'f')
)
SELECT DISTINCT ON (oid, kind, ref_schema, ref_name, columns) oid, kind, ref_schema, ref_name, columns, ref_columns
FROM keys
ORDER BY oid, kind, ref_schema, ref_name, columns, conname
""").bindparams(bindparam("oids", type_=ARRAY(BigInteger)))

# Most common values of text columns, from the statistics ANALYZE keeps (readable only for
# columns the role may read). Partitioned parents only have inherited statistics.
_COMMON_VALUES = text("""
SELECT DISTINCT ON (c.oid, s.attname)
       c.oid::bigint AS oid, s.attname::text AS column_name, s.n_distinct,
       s.most_common_vals::text::text[] AS common_values
FROM pg_stats s
JOIN pg_namespace n ON n.nspname = s.schemaname
JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = s.tablename
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = s.attname
JOIN pg_type t ON t.oid = a.atttypid
WHERE c.oid::bigint = ANY(:oids) AND t.typcategory = 'S' AND s.most_common_vals IS NOT NULL
ORDER BY c.oid, s.attname, s.inherited DESC
""").bindparams(bindparam("oids", type_=ARRAY(BigInteger)))


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def short_type(pg_type: str) -> str:
    """Compact type names for documents (fewer prompt tokens, same meaning)."""
    for pattern, replacement in _TYPE_ABBREVIATIONS:
        pg_type = pattern.sub(replacement, pg_type)
    return pg_type


def is_sensitive_column(name: str) -> bool:
    return bool(_SENSITIVE_COLUMN.search(name))


def values_from_statistics(
    common_values: list[str], n_distinct: float, row_estimate: int
) -> tuple[tuple[str, ...], int]:
    """Example values from pg_stats, if the column has few distinct values.

    `n_distinct` is a count when positive and a fraction of the rows (negated) when negative.
    Returns (values, number not shown), or ((), 0) when the column isn't categorical.
    """
    distinct = n_distinct if n_distinct > 0 else -n_distinct * max(row_estimate, 0)
    if not common_values or distinct > MAX_DISTINCT_VALUES:
        return (), 0
    if any(len(v) > VALUE_MAX_CHARS for v in common_values):
        return (), 0  # free text, not categories
    shown = tuple(common_values[:SHOWN_VALUES])
    return shown, max(0, round(distinct) - len(shown))


def values_from_rows(values: list[str | None], unique_allowed: bool) -> tuple[tuple[str, ...], int]:
    """Example values from every row of a small table (most common first).

    `unique_allowed` is False for tables about people: a column that is different on every
    row there is someone's name or handle, not a category.
    """
    present = [v for v in values if v not in (None, "")]
    counts = Counter(present)
    if not counts or len(counts) > MAX_DISTINCT_VALUES:
        return (), 0
    if any(len(v) > VALUE_MAX_CHARS for v in counts):
        return (), 0
    if not unique_allowed and len(counts) == len(present) and len(present) > 1:
        return (), 0
    ordered = [v for v, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    return tuple(ordered[:SHOWN_VALUES]), max(0, len(ordered) - SHOWN_VALUES)


def _value_candidates(table: TableInfo) -> list[ColumnInfo]:
    return [c for c in table.columns if c.is_text and not c.values and not is_sensitive_column(c.name)]


async def _read_small_table_values(conn: AsyncConnection, table: TableInfo) -> None:
    """Read up to SMALL_TABLE_ROWS rows of the candidate columns; skip if the table is bigger."""
    columns = _value_candidates(table)
    if not columns:
        return
    selected = ", ".join(
        f"left({quote_ident(c.name)}::text, {VALUE_MAX_CHARS + 1})" for c in columns
    )
    query = f"SELECT {selected} FROM {quote_ident(table.schema)}.{quote_ident(table.name)} LIMIT {SMALL_TABLE_ROWS + 1}"
    try:
        async with conn.begin_nested():  # a failing read must not abort the transaction
            rows = (await conn.execute(text(query))).all()
    except Exception as exc:
        logger.info("Example values skipped for %s: %s", table.display_name, type(exc).__name__)
        return
    if len(rows) > SMALL_TABLE_ROWS:
        return
    unique_allowed = not _PEOPLE_TABLE.search(table.name)
    for i, column in enumerate(columns):
        column.values, column.more_values = values_from_rows([row[i] for row in rows], unique_allowed)


def _clip(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


async def _read_catalog(conn: AsyncConnection) -> IntrospectedSchema:
    relations = (
        await conn.execute(_RELATIONS, {"excluded_schemas": list(PLATFORM_SCHEMAS), "limit": MAX_INDEXED_TABLES + 1})
    ).mappings().all()
    truncated = len(relations) > MAX_INDEXED_TABLES
    tables: dict[int, TableInfo] = {
        r["oid"]: TableInfo(
            schema=r["schema_name"],
            name=r["name"],
            kind=r["kind"],
            columns=[],
            row_estimate=r["row_estimate"],
            comment=_clip(r["comment"], 300),
        )
        for r in relations[:MAX_INDEXED_TABLES]
    }
    if not tables:
        return IntrospectedSchema([], truncated)
    oids = list(tables)

    for r in (await conn.execute(_COLUMNS, {"oids": oids})).mappings():
        labels = tuple(r["enum_labels"] or ())
        tables[r["oid"]].columns.append(
            ColumnInfo(
                name=r["name"],
                type=short_type(r["type"]),
                nullable=r["nullable"],
                comment=_clip(r["comment"], 120),
                values=labels[:MAX_DISTINCT_VALUES],
                more_values=max(0, len(labels) - MAX_DISTINCT_VALUES),
                is_text=r["is_text"],
            )
        )

    for r in (await conn.execute(_CONSTRAINTS, {"oids": oids})).mappings():
        table = tables[r["oid"]]
        if r["kind"] == "p":
            key = set(r["columns"])
            for column in table.columns:
                column.primary_key = column.name in key
        else:
            table.foreign_keys.append(
                ForeignKeyInfo(r["columns"], display_name(r["ref_schema"], r["ref_name"]), r["ref_columns"])
            )

    # Small or never-analysed tables are read directly; the rest use pg_stats.
    small_oids = [oid for oid, t in tables.items() if t.kind in ("r", "m") and t.row_estimate <= SMALL_TABLE_ROWS]
    for r in (await conn.execute(_COMMON_VALUES, {"oids": oids})).mappings():
        if r["oid"] in small_oids or is_sensitive_column(r["column_name"]):
            continue
        table = tables[r["oid"]]
        column = next((c for c in table.columns if c.name == r["column_name"]), None)
        if column is not None and not column.values:
            column.values, column.more_values = values_from_statistics(
                r["common_values"], r["n_distinct"], table.row_estimate
            )
    for oid in small_oids:
        await _read_small_table_values(conn, tables[oid])

    return IntrospectedSchema(list(tables.values()), truncated)


async def introspect(url: str) -> IntrospectedSchema:
    """Read the target database's structure (see the module docstring)."""
    async with open_target_engine(url) as engine, engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(text(f"SET LOCAL statement_timeout = {INTROSPECTION_TIMEOUT_MS}"))
        return await _read_catalog(conn)
