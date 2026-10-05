"""API tests through FastAPI with dependency overrides (no real auth or database)."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.endpoints import query as query_endpoint
from app.core import errors
from app.core.exceptions import Conflict, LimitReached, NotFound
from app.db.session import get_db
from app.main import app
from app.services import users as user_service
from app.services.question_context import QuestionContext

USER = SimpleNamespace(id=uuid.uuid4(), clerk_id="user_real")


class _NoDb:
    """Stands in for AsyncSession where the code path under test never touches it."""


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: _NoDb()
    app.dependency_overrides[deps.get_token_claims] = lambda: {"sub": "user_real", "fea": []}
    app.dependency_overrides[deps.get_current_user] = lambda: USER
    for limiter in deps._rate_limiters.values():
        limiter.reset()
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


QUESTION = {"connection_id": str(uuid.uuid4()), "nl_query": "total sales?"}


@pytest.fixture
def question_steps(monkeypatch):
    """Mocks the checks before streaming; records whether the question was reserved."""
    calls: dict[str, object] = {"reserved": False, "owned": True, "indexed": True, "reserve_error": None}

    async def get_for_user(session, user_id, connection_id):
        if not calls["owned"]:
            raise NotFound(errors.CONNECTION_NOT_FOUND)
        indexed = calls["indexed"]
        return SimpleNamespace(
            id=connection_id,
            encrypted_conn_string="x",
            indexed_at="2026-10-05T00:00:00Z" if indexed else None,
            table_count=10 if indexed else None,
        )

    async def reserve(session, user_id, connection_id, question, monthly_limit, follow_up_of=None):
        calls["reserved"] = True
        calls["monthly_limit"] = monthly_limit
        calls["follow_up_of"] = follow_up_of
        if calls["reserve_error"]:
            raise calls["reserve_error"]
        return uuid.uuid4()

    async def resume(session, user_id, connection_id, log_id):
        calls["resumed"] = log_id
        if calls.get("resume_error"):
            raise calls["resume_error"]
        return SimpleNamespace(id=log_id, nl_query="who are our best customers?", follow_up_of=None)

    async def remember_clarification(session, user_id, log, answer):
        calls["remembered"] = answer

    async def load_context(session, user_id, connection_id, question, follow_up_of):
        return QuestionContext()

    async def run_pipeline(
        session, connection_id, url, question, outcome, clarification=None, context=None, reuse_for_user=None
    ):
        calls["pipeline"] = (question, clarification)
        calls["reuse_for_user"] = reuse_for_user
        yield {"type": "results", "rows": []}
        yield {"type": "done"}

    monkeypatch.setattr(query_endpoint.connection_service, "get_for_user", get_for_user)
    monkeypatch.setattr(query_endpoint.query_meter, "resume", resume)
    monkeypatch.setattr(query_endpoint.knowledge, "remember_clarification", remember_clarification)
    monkeypatch.setattr(query_endpoint.question_context, "load", load_context)
    monkeypatch.setattr(query_endpoint, "run_pipeline", run_pipeline)
    monkeypatch.setattr(query_endpoint, "spawn", lambda coro, name: coro.close())
    monkeypatch.setattr(query_endpoint.query_meter, "reserve", reserve)
    return calls


class TestQuestionGate:
    """Rejections before the pipeline don't count toward the plan (business-logic.md §2)."""

    def test_foreign_connection_is_404_and_not_counted(self, client, question_steps):
        question_steps["owned"] = False
        assert client.post("/api/v1/query/", json=QUESTION).status_code == 404
        assert question_steps["reserved"] is False

    def test_unindexed_connection_is_400_and_not_counted(self, client, question_steps):
        question_steps["indexed"] = False
        assert client.post("/api/v1/query/", json=QUESTION).status_code == 400
        assert question_steps["reserved"] is False

    def test_an_indexed_connection_with_no_tables_is_400_and_not_counted(self, client, question_steps, monkeypatch):
        async def get_for_user(session, user_id, connection_id):
            return SimpleNamespace(id=connection_id, encrypted_conn_string="x", indexed_at="2026-10-05", table_count=0)

        monkeypatch.setattr(query_endpoint.connection_service, "get_for_user", get_for_user)
        assert client.post("/api/v1/query/", json=QUESTION).status_code == 400
        assert question_steps["reserved"] is False

    def test_monthly_limit_comes_from_the_plan(self, client, question_steps):
        question_steps["reserve_error"] = LimitReached("QUERY_LIMIT_REACHED")
        response = client.post("/api/v1/query/", json=QUESTION)
        assert response.status_code == 403
        assert response.json() == {"detail": "QUERY_LIMIT_REACHED"}
        assert question_steps["monthly_limit"] == 50  # free plan

    def test_one_question_at_a_time(self, client, question_steps):
        question_steps["reserve_error"] = Conflict(errors.QUESTION_IN_FLIGHT)
        response = client.post("/api/v1/query/", json=QUESTION)
        assert response.status_code == 409
        assert response.json() == {"detail": errors.QUESTION_IN_FLIGHT}

    def test_answering_a_clarification_resumes_instead_of_reserving(self, client, question_steps):
        question_id = str(uuid.uuid4())
        body = {**QUESTION, "clarification": {"question_id": question_id, "answer": "By total spent"}}
        response = client.post("/api/v1/query/", json=body)
        assert response.status_code == 200
        assert question_steps["reserved"] is False
        assert str(question_steps["resumed"]) == question_id
        # The stored question is used, with the user's answer alongside, and it's remembered.
        assert question_steps["pipeline"] == ("who are our best customers?", "By total spent")
        assert question_steps["remembered"] == "By total spent"
        # The answer carries the question's id, so the browser can follow up or verify it.
        assert f'"question_id": "{question_id}"' in response.text

    def test_follow_up_link_is_passed_to_the_meter(self, client, question_steps):
        earlier = str(uuid.uuid4())
        response = client.post("/api/v1/query/", json={**QUESTION, "follow_up_of": earlier})
        assert response.status_code == 200
        assert str(question_steps["follow_up_of"]) == earlier

    def test_earlier_answers_are_reused_only_from_the_same_user(self, client, question_steps):
        assert client.post("/api/v1/query/", json=QUESTION).status_code == 200
        assert question_steps["reuse_for_user"] == USER.id

    def test_fresh_asks_the_model_again(self, client, question_steps):
        assert client.post("/api/v1/query/", json={**QUESTION, "fresh": True}).status_code == 200
        assert question_steps["reuse_for_user"] is None
        assert question_steps["reserved"] is True  # a new query still counts

    def test_expired_clarification_is_404(self, client, question_steps):
        question_steps["resume_error"] = NotFound(errors.CLARIFICATION_EXPIRED)
        body = {**QUESTION, "clarification": {"question_id": str(uuid.uuid4()), "answer": "x"}}
        response = client.post("/api/v1/query/", json=body)
        assert response.status_code == 404
        assert response.json() == {"detail": errors.CLARIFICATION_EXPIRED}


class TestRateLimits:
    def test_connection_tests_are_limited_per_user(self, client):
        url = "/api/v1/connections/test"
        codes = [client.post(url, json={"conn_string": "http://x/"}).status_code for _ in range(11)]
        assert codes[:10] == [200] * 10
        assert codes[10] == 429
        assert client.post(url, json={"conn_string": "http://x/"}).json() == {"detail": errors.TOO_MANY_REQUESTS}


class TestPlanLimits:
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


class _HistoryDb:
    """Answers the history endpoint's count and page queries."""

    def __init__(self, logs):
        self.logs = logs

    async def scalar(self, statement):
        return len(self.logs)

    async def scalars(self, statement):
        return iter(self.logs)


def test_history_items_say_whether_their_answer_is_verified(client, monkeypatch):
    def log(question):
        return SimpleNamespace(
            id=uuid.uuid4(), connection_id=uuid.uuid4(), nl_query=question, generated_sql="SELECT 1",
            row_count=1, exec_time_ms=5, status="success", error_message=None, follow_up_of=None,
            created_at="2026-10-05T00:00:00Z",
        )

    verified, plain = log("total sales?"), log("top artists?")
    db = _HistoryDb([verified, plain])
    app.dependency_overrides[get_db] = lambda: db

    async def verified_log_ids(session, user_id, logs):
        assert session is db and user_id == USER.id
        return {verified.id}

    monkeypatch.setattr(query_endpoint.verified_queries, "verified_log_ids", verified_log_ids)
    response = client.get("/api/v1/query/history")

    assert response.status_code == 200
    assert [item["verified"] for item in response.json()["items"]] == [True, False]


def test_history_items_carry_the_restatement_and_the_bare_statement(client, monkeypatch):
    entry = SimpleNamespace(
        id=uuid.uuid4(), connection_id=uuid.uuid4(), nl_query="total sales?",
        generated_sql="-- Intent: number\n-- Understood: Total of all sales.\nSELECT 1\n-- Follow-up: By month?",
        row_count=1, exec_time_ms=5, status="success", error_message=None, follow_up_of=None,
        created_at="2026-10-05T00:00:00Z",
    )
    app.dependency_overrides[get_db] = lambda: _HistoryDb([entry])

    async def verified_log_ids(session, user_id, logs):
        return set()

    monkeypatch.setattr(query_endpoint.verified_queries, "verified_log_ids", verified_log_ids)
    item = client.get("/api/v1/query/history").json()["items"][0]

    assert item["understood"] == "Total of all sales."
    assert item["sql"] == "SELECT 1"
    assert item["generated_sql"] == entry.generated_sql  # the full reply is still there for exports


class TestSavedAnswer:
    def test_returns_the_users_saved_answer(self, client, monkeypatch):
        question_id = uuid.uuid4()
        saved = {"answer": {"headline": "4 projects."}, "columns": ["count"], "rows": [[4]], "row_count": 1, "truncated": False}

        async def get(session, user_id, log_id):
            assert user_id == USER.id and log_id == question_id  # scoped to the signed-in user
            return saved

        monkeypatch.setattr(query_endpoint.answer_snapshot, "get", get)
        response = client.get(f"/api/v1/query/{question_id}/answer")

        assert response.status_code == 200
        assert response.json() == saved

    def test_a_question_without_a_saved_answer_is_404_with_next_steps(self, client, monkeypatch):
        async def get(session, user_id, log_id):
            raise NotFound(errors.ANSWER_NOT_SAVED)

        monkeypatch.setattr(query_endpoint.answer_snapshot, "get", get)
        response = client.get(f"/api/v1/query/{uuid.uuid4()}/answer")

        assert response.status_code == 404
        assert response.json()["detail"] == errors.ANSWER_NOT_SAVED


def test_requests_without_a_token_are_rejected():
    response = TestClient(app).get("/api/v1/connections/")
    assert response.status_code in (401, 403)
