"""Unit tests for target_db — URL normalisation and the outbound-host (SSRF) guard."""
import asyncio

import pytest

from app.core import errors
from app.core.config import settings
from app.core.exceptions import InvalidInput
from app.services import target_db
from app.services.target_db import ensure_host_allowed, normalize_url


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        "raw",
        [
            "postgres://u:p@db.example.com/app",
            "postgresql://u:p@db.example.com/app",
            "postgresql+psycopg2://u:p@db.example.com/app",
            "postgresql+psycopg://u:p@db.example.com/app",
            "postgresql+asyncpg://u:p@db.example.com/app",
        ],
    )
    def test_schemes_become_asyncpg(self, raw):
        assert normalize_url(raw) == "postgresql+asyncpg://u:p@db.example.com/app"

    def test_strips_libpq_only_params(self):
        url = "postgresql://u:p@h/db?sslmode=require&channel_binding=require&application_name=x"
        assert normalize_url(url) == "postgresql+asyncpg://u:p@h/db"

    def test_keeps_supported_params(self):
        assert normalize_url("postgresql://u:p@h/db?sslmode=require&ssl=true") == "postgresql+asyncpg://u:p@h/db?ssl=true"

    @pytest.mark.parametrize("raw", ["mysql://u:p@h/db", "http://evil.example.com", "file:///etc/passwd", "h/db"])
    def test_rejects_non_postgres(self, raw):
        with pytest.raises(InvalidInput) as info:
            normalize_url(raw)
        assert info.value.message == errors.UNSUPPORTED_SCHEME


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ALLOW_PRIVATE_DB_HOSTS", False)


def _resolving_to(monkeypatch, *addresses: str):
    async def fake_resolve(host, port):
        return list(addresses)

    monkeypatch.setattr(target_db, "_resolve", fake_resolve)


def _check(url: str) -> None:
    asyncio.run(ensure_host_allowed(url))


class TestHostGuard:
    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.10", "169.254.169.254", "::1", "fc00::1", "0.0.0.0"],
    )
    def test_blocks_non_public_addresses(self, production, monkeypatch, address):
        _resolving_to(monkeypatch, address)
        with pytest.raises(InvalidInput) as info:
            _check("postgresql+asyncpg://u:p@internal.example/db")
        assert info.value.message == errors.HOST_NOT_ALLOWED

    def test_blocks_if_any_resolved_address_is_private(self, production, monkeypatch):
        _resolving_to(monkeypatch, "34.120.10.10", "10.0.0.7")
        with pytest.raises(InvalidInput):
            _check("postgresql+asyncpg://u:p@mixed.example/db")

    def test_allows_public_addresses(self, production, monkeypatch):
        _resolving_to(monkeypatch, "34.120.10.10")
        _check("postgresql+asyncpg://u:p@ep-cool.neon.tech/db")  # no exception

    def test_development_allows_private_hosts(self, monkeypatch):
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        _resolving_to(monkeypatch, "127.0.0.1")
        _check("postgresql+asyncpg://u:p@localhost/db")

    def test_explicit_override_allows_private_hosts(self, production, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_PRIVATE_DB_HOSTS", True)
        _resolving_to(monkeypatch, "10.0.0.5")
        _check("postgresql+asyncpg://u:p@db.internal/db")

    def test_unresolvable_host_is_left_to_the_connection_attempt(self, production, monkeypatch):
        async def fail(host, port):
            raise OSError("no such host")

        monkeypatch.setattr(target_db, "_resolve", fail)
        _check("postgresql+asyncpg://u:p@does-not-exist.invalid/db")

    def test_missing_host_is_invalid(self, production):
        with pytest.raises(InvalidInput) as info:
            _check("postgresql+asyncpg:///db")
        assert info.value.message == errors.INVALID_CONNECTION_STRING
