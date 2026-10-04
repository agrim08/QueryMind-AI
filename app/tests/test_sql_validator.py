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
