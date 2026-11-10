"""Unit tests for sql_validator — run with: pytest app/tests/test_sql_validator.py -v"""
import pytest

from app.services.sql_validator import validate_sql


class TestBlocklist:
    def test_drop_blocked(self):
        result = validate_sql("DROP TABLE users")
        assert not result.is_valid
        assert "DROP" in result.error

    def test_delete_blocked(self):
        result = validate_sql("DELETE FROM users WHERE id = 1")
        assert not result.is_valid
        assert "DELETE" in result.error

    def test_insert_blocked(self):
        result = validate_sql("INSERT INTO users (name) VALUES ('hack')")
        assert not result.is_valid

    def test_update_blocked(self):
        result = validate_sql("UPDATE users SET name = 'x'")
        assert not result.is_valid

    def test_alter_blocked(self):
        result = validate_sql("ALTER TABLE users ADD COLUMN foo TEXT")
        assert not result.is_valid

    def test_truncate_blocked(self):
        result = validate_sql("TRUNCATE TABLE users")
        assert not result.is_valid

    def test_create_blocked(self):
        result = validate_sql("CREATE TABLE foo (id INT)")
        assert not result.is_valid

    def test_grant_blocked(self):
        result = validate_sql("GRANT ALL ON users TO hacker")
        assert not result.is_valid


class TestSideEffectingSelects:
    """SELECTs that would still change state or harm the user's database."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * INTO backup_users FROM users",
            "SELECT id FROM orders FOR SHARE",
            "SELECT id FROM orders FOR NO KEY UPDATE",
            "SELECT id FROM orders FOR KEY SHARE",
            "SELECT id FROM orders FOR UPDATE",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity",
            "SELECT pg_cancel_backend(42)",
            "SELECT pg_sleep(30)",
            "SELECT pg_sleep_for('1 minute')",
            "SELECT nextval('orders_id_seq')",
            "SELECT setval('orders_id_seq', 1)",
            "SELECT set_config('search_path', 'evil', false)",
            "SELECT * FROM dblink('host=10.0.0.5', 'SELECT 1') AS t(x int)",
            "SELECT lo_import('/etc/passwd')",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT pg_ls_dir('.')",
            "SELECT pg_advisory_lock(1)",
            "SELECT query_to_xml('SELECT 1', true, true, '')",
            "SELECT PG_SLEEP (5)",
        ],
    )
    def test_blocked(self, sql):
        result = validate_sql(sql)
        assert not result.is_valid, sql


class TestNoFalsePositives:
    """Ordinary read queries that resemble blocked patterns must still pass."""

    @pytest.mark.parametrize(
        "sql",
        [
            'SELECT "into_date", "lo_value", "sleep_hours" FROM metrics',
            "SELECT LOWER(name) FROM users",
            "SELECT pg_size_pretty(pg_total_relation_size('users'))",
            'SELECT "for_share_count" FROM stats',
            "SELECT COUNT(*) FROM users WHERE shared = true",
        ],
    )
    def test_allowed(self, sql):
        result = validate_sql(sql)
        assert result.is_valid, f"{sql} -> {result.error}"


class TestValidSelects:
    def test_simple_select(self):
        result = validate_sql("SELECT * FROM users")
        assert result.is_valid

    def test_select_with_where(self):
        result = validate_sql("SELECT id, name FROM users WHERE active = true")
        assert result.is_valid

    def test_select_with_join(self):
        result = validate_sql(
            "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
        )
        assert result.is_valid

    def test_select_with_aggregation(self):
        result = validate_sql("SELECT COUNT(*) FROM users GROUP BY status")
        assert result.is_valid

    def test_select_with_subquery(self):
        result = validate_sql(
            "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE total > 100)"
        )
        assert result.is_valid


class TestEdgeCases:
    def test_empty_sql(self):
        result = validate_sql("")
        assert not result.is_valid

    def test_multiple_statements(self):
        result = validate_sql("SELECT 1; SELECT 2")
        assert not result.is_valid

    def test_known_tables_valid(self):
        result = validate_sql(
            "SELECT * FROM users", known_tables=["users", "orders"]
        )
        assert result.is_valid

    def test_known_tables_unknown_table(self):
        result = validate_sql(
            "SELECT * FROM secret_table", known_tables=["users", "orders"]
        )
        assert not result.is_valid
        assert "secret_table" in result.error

    def test_schema_prefixed_table(self):
        result = validate_sql(
            "SELECT * FROM public.users", known_tables=["users"]
        )
        assert result.is_valid  # strips schema prefix


class TestKnownTablesFromKeyword:
    """FROM also appears inside expressions; only FROM/JOIN clauses name tables (eval case c08)."""

    KNOWN = ["invoice", "customer"]

    @pytest.mark.parametrize(
        "sql",
        [
            'SELECT SUM("i"."total") FROM "invoice" AS "i" WHERE EXTRACT(YEAR FROM "i"."invoice_date") = 2023',
            "SELECT extract(month from invoice_date), count(*) FROM invoice GROUP BY 1",
            "SELECT SUBSTRING(billing_city FROM 1 FOR 3) FROM invoice",
            "SELECT TRIM(BOTH ' ' FROM billing_city) FROM invoice",
            "SELECT OVERLAY(billing_city PLACING 'X' FROM 1 FOR 1) FROM invoice",
            "SELECT * FROM invoice WHERE billing_state IS DISTINCT FROM 'CA'",
            "SELECT * FROM invoice WHERE billing_state IS NOT DISTINCT FROM NULL",
            "SELECT * FROM invoice WHERE billing_city = 'from paris' OR billing_city = 'join us'",
            "SELECT EXTRACT(YEAR FROM i.invoice_date) FROM invoice i JOIN customer c USING (customer_id)",
        ],
    )
    def test_expression_from_is_not_a_table(self, sql):
        result = validate_sql(sql, known_tables=self.KNOWN)
        assert result.is_valid, result.error

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT EXTRACT(YEAR FROM invoice_date) FROM secret_table",
            "SELECT * FROM invoice JOIN secret_table USING (customer_id)",
            # A subquery inside EXTRACT still has its tables checked.
            "SELECT EXTRACT(YEAR FROM (SELECT max(created_at) FROM secret_table)) FROM invoice",
            "SELECT * FROM invoice WHERE customer_id IN (SELECT id FROM secret_table)",
            # A string literal can't hide a real table reference after it.
            "SELECT 'from x' AS note FROM secret_table",
        ],
    )
    def test_unknown_tables_are_still_rejected(self, sql):
        result = validate_sql(sql, known_tables=self.KNOWN)
        assert not result.is_valid
        assert "secret_table" in result.error


class TestKnownTablesSchemaQualified:
    """Tables outside `public` are listed as schema.table (Phase 1.3); `public` ones bare."""

    KNOWN = ["invoice", "sales.orders"]

    @pytest.mark.parametrize(
        "sql",
        [
            'SELECT * FROM "public"."invoice"',
            "SELECT * FROM public.invoice",
            'SELECT * FROM "sales"."orders" o JOIN "invoice" i ON i.id = o.invoice_id',
            "SELECT * FROM sales.orders",
            'SELECT * FROM "Sales"."Orders"',
        ],
    )
    def test_listed_tables_are_accepted(self, sql):
        result = validate_sql(sql, known_tables=self.KNOWN)
        assert result.is_valid, result.error

    @pytest.mark.parametrize(
        "sql",
        [
            'SELECT * FROM "hr"."orders"',  # right table name, wrong schema
            "SELECT * FROM public.orders",  # orders isn't in public
            'SELECT * FROM "sales"."invoice"',  # invoice isn't in sales
            "SELECT * FROM salaries",
        ],
    )
    def test_unlisted_tables_are_rejected(self, sql):
        assert not validate_sql(sql, known_tables=self.KNOWN).is_valid


class TestCommonTableExpressions:
    """WITH … SELECT is allowed (approved 2026-10-09, known gap #20); writes inside CTEs are not."""

    KNOWN = ["invoice", "invoice_line", "track", "genre"]

    @pytest.mark.parametrize("sql", [
        'WITH monthly AS (SELECT date_trunc(\'month\', "invoice_date") AS m, SUM("total") AS t FROM "invoice" GROUP BY 1) '
        'SELECT m, t FROM monthly ORDER BY m',
        "WITH a AS (SELECT genre_id FROM track), b AS (SELECT genre_id FROM genre) SELECT * FROM a JOIN b USING (genre_id)",
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) SELECT i FROM n",
        'WITH "Sales" AS MATERIALIZED (SELECT * FROM invoice) SELECT * FROM "Sales"',
        "with lower_case as (select * from invoice) select count(*) from lower_case",
    ])
    def test_read_only_ctes_are_accepted(self, sql):
        assert validate_sql(sql, known_tables=self.KNOWN).is_valid

    @pytest.mark.parametrize("sql", [
        "WITH gone AS (DELETE FROM invoice RETURNING *) SELECT * FROM gone",
        "WITH x AS (SELECT 1) INSERT INTO invoice SELECT * FROM x",
        "WITH x AS (UPDATE invoice SET total = 0 RETURNING *) SELECT * FROM x",
        "WITH x AS (SELECT * FROM invoice) SELECT * INTO copy FROM x",
        "WITH x AS (SELECT pg_sleep(10)) SELECT * FROM x",
    ])
    def test_writes_and_side_effects_inside_ctes_are_rejected(self, sql):
        assert not validate_sql(sql, known_tables=self.KNOWN).is_valid

    def test_unknown_tables_inside_a_cte_are_still_rejected(self):
        result = validate_sql("WITH x AS (SELECT * FROM secrets) SELECT * FROM x", known_tables=self.KNOWN)
        assert not result.is_valid and result.unknown_tables == ("secrets",)

    def test_a_cte_name_only_counts_when_the_query_starts_with_with(self):
        # Without a WITH clause, "monthly" is just an unknown table.
        result = validate_sql("SELECT * FROM monthly", known_tables=self.KNOWN)
        assert not result.is_valid and result.unknown_tables == ("monthly",)
