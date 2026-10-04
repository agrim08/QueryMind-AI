"""Knowledge endpoints through FastAPI, with auth, ownership, the database and Gemini mocked."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from app.api import deps
from app.api.endpoints import knowledge as endpoint
from app.core import errors
from app.db.session import get_db
from app.main import app
from app.services import knowledge as knowledge_service

USER = SimpleNamespace(id=uuid.uuid4(), clerk_id="user_real")
CONNECTION = SimpleNamespace(id=uuid.uuid4(), user_id=USER.id)
BASE = f"/api/v1/connections/{CONNECTION.id}/knowledge"


class FakeDb:
    async def commit(self):
        pass

    async def refresh(self, obj):
        pass

    async def delete(self, obj):
        pass


@pytest.fixture
def state(monkeypatch):
    """The fake connection's stored knowledge, and what the services were asked to do."""
    context = SimpleNamespace(description="We sell music online.", starter_questions=[], setup_calls_day=None, setup_calls_used=0)
    s = {"context": context, "items": [], "replaced": None, "extract_error": None}

    async def get_context(db, user_id, connection_id):
        return context

    async def list_items(db, user_id, connection_id):
        return s["items"]

    async def list_verified(db, user_id, connection_id):
        return []

    async def all_tables(db, connection_id):
        return [SimpleNamespace(table_name="invoice", doc="Table: invoice")]

    async def extract(description, docs):
        if s["extract_error"]:
            raise s["extract_error"]
        s["extracted_from"] = description
        return [knowledge_service.ItemDraft("metric", "revenue", "SUM(invoice.total)")], ["What was revenue last month?"]

    async def replace_ai_items(db, user_id, connection_id, drafts):
        s["replaced"] = drafts

    async def get_item(db, user_id, connection_id, item_id):
        return s["items"][0]

    monkeypatch.setattr(endpoint.knowledge, "get_context", get_context)
    monkeypatch.setattr(endpoint.knowledge, "list_items", list_items)
    monkeypatch.setattr(endpoint.knowledge, "replace_ai_items", replace_ai_items)
    monkeypatch.setattr(endpoint.knowledge, "get_item", get_item)
    monkeypatch.setattr(endpoint.verified_queries, "list_for_connection", list_verified)
    monkeypatch.setattr(endpoint, "all_tables", all_tables)
    monkeypatch.setattr(endpoint.business_setup, "extract_knowledge", extract)
    return s


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: FakeDb()
    app.dependency_overrides[deps.get_token_claims] = lambda: {"sub": "user_real", "fea": []}
    app.dependency_overrides[deps.get_current_user] = lambda: USER
    app.dependency_overrides[deps.get_owned_connection] = lambda: CONNECTION
    for limiter in deps._rate_limiters.values():
        limiter.reset()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _item(source: str = "ai"):
    return SimpleNamespace(
        id=uuid.uuid4(), kind="metric", name="revenue", definition="SUM(invoice.total)",
        source=source, updated_at=datetime.now(timezone.utc),
    )


def test_extract_uses_the_saved_description_and_a_setup_call(client, state):
    response = client.post(f"{BASE}/extract")
    assert response.status_code == 200
    assert state["extracted_from"] == "We sell music online."
    assert state["replaced"] == [knowledge_service.ItemDraft("metric", "revenue", "SUM(invoice.total)")]
    body = response.json()
    assert body["starter_questions"] == ["What was revenue last month?"]
    assert body["setup_calls_left"] == knowledge_service.SETUP_CALLS_PER_DAY - 1


def test_setup_calls_are_capped_per_day(client, state):
    state["context"].setup_calls_day = datetime.now(timezone.utc).date()
    state["context"].setup_calls_used = knowledge_service.SETUP_CALLS_PER_DAY
    response = client.post(f"{BASE}/extract")
    assert response.status_code == 429
    assert response.json() == {"detail": errors.SETUP_CALLS_USED}


def test_gemini_quota_says_busy(client, state):
    state["extract_error"] = genai_errors.ClientError(429, {"error": {"code": 429, "message": "quota"}})
    response = client.post(f"{BASE}/extract")
    assert response.status_code == 502
    assert response.json() == {"detail": errors.AI_BUSY}


def test_editing_an_ai_definition_makes_it_the_users(client, state):
    state["items"] = [_item("ai")]
    response = client.patch(f"{BASE}/items/{state['items'][0].id}", json={"definition": "SUM(invoice.total) - refunds"})
    assert response.status_code == 200
    assert response.json()["source"] == "user"
    assert response.json()["definition"] == "SUM(invoice.total) - refunds"


@pytest.mark.parametrize(
    "path, method, body",
    [
        ("/description", "put", {"description": "x" * 4001}),
        ("/items", "post", {"kind": "secret", "name": "a", "definition": "b"}),
        ("/items", "post", {"kind": "term", "name": "", "definition": "b"}),
    ],
)
def test_inputs_are_bounded(client, state, path, method, body):
    assert getattr(client, method)(f"{BASE}{path}", json=body).status_code == 422
