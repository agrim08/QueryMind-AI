"""API tests through FastAPI with dependency overrides (no real auth or database)."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.db.session import get_db
from app.main import app
from app.services import users as user_service

USER = SimpleNamespace(id=uuid.uuid4(), clerk_id="user_real")


class _NoDb:
    """Stands in for AsyncSession where the code path under test never touches it."""


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: _NoDb()
    app.dependency_overrides[deps.get_token_claims] = lambda: {"sub": "user_real", "fea": []}
    app.dependency_overrides[deps.get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestUserSync:
    def test_identity_comes_from_the_token_not_the_body(self, client, monkeypatch):
        captured = {}

        async def fake_upsert(session, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                id=uuid.uuid4(), clerk_id=kwargs["clerk_id"], email=kwargs["email"],
                full_name=None, avatar_url=None, created_at="2026-10-04T00:00:00Z",
            )

        monkeypatch.setattr(user_service, "upsert_from_clerk", fake_upsert)
        response = client.post(
            "/api/v1/users/sync", json={"clerk_id": "user_victim", "email": "me@example.com"}
        )

        assert response.status_code == 200
        assert captured["clerk_id"] == "user_real"
        assert response.json()["clerk_id"] == "user_real"


class TestPlanLimits:
    def test_question_limit_rejects_before_any_work(self, client, monkeypatch):
        async def at_limit(session, user_id):
            return 50

        monkeypatch.setattr(deps.usage, "count_questions_this_month", at_limit)
        response = client.post("/api/v1/query/", json={"connection_id": str(uuid.uuid4()), "nl_query": "hi"})

        assert response.status_code == 403
        assert response.json() == {"detail": "QUERY_LIMIT_REACHED"}

    def test_connection_limit(self, client, monkeypatch):
        async def one_owned(session, user_id):
            return 1

        monkeypatch.setattr(deps.usage, "count_connections", one_owned)
        response = client.post(
            "/api/v1/connections/", json={"name": "prod", "connection_string": "postgresql://u:p@h/db"}
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "CONNECTION_LIMIT_REACHED"}


class TestValidation:
    @pytest.fixture(autouse=True)
    def under_quota(self, monkeypatch):
        # Route dependencies (the quota check) run before body validation errors are raised.
        async def none_used(session, user_id):
            return 0

        monkeypatch.setattr(deps.usage, "count_questions_this_month", none_used)

    @pytest.mark.parametrize("question", ["", "   ", "x" * 2001])
    def test_question_is_bounded_with_a_readable_message(self, client, question):
        response = client.post("/api/v1/query/", json={"connection_id": str(uuid.uuid4()), "nl_query": question})
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)
        assert response.json()["detail"].startswith("Invalid nl_query")

    def test_history_page_size_is_capped(self, client):
        assert client.get("/api/v1/query/history?page_size=1000").status_code == 422

    def test_test_connection_rejects_non_postgres_urls(self, client):
        response = client.post("/api/v1/connections/test", json={"conn_string": "http://169.254.169.254/"})
        assert response.status_code == 200
        assert response.json()["ok"] is False


def test_requests_without_a_token_are_rejected():
    response = TestClient(app).get("/api/v1/connections/")
    assert response.status_code in (401, 403)
