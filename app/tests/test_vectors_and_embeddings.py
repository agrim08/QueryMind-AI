"""Unit tests for the halfvec column type and batched embeddings."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.ai_config import EMBED_BATCH_SIZE, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from app.db.types import HalfVector
from app.models.models import SchemaElement
from app.services import embeddings


class TestHalfVector:
    def test_column_spec(self):
        assert HalfVector(768).get_col_spec() == "halfvec(768)"

    def test_values_are_sent_as_vector_text(self):
        process = HalfVector(3).bind_processor(postgresql.dialect())
        assert process([0.5, -1, 2]) == "[0.5,-1.0,2.0]"
        assert process(None) is None

    def test_wrong_dimension_is_rejected(self):
        process = HalfVector(3).bind_processor(postgresql.dialect())
        with pytest.raises(ValueError):
            process([1.0, 2.0])

    def test_cosine_distance_casts_text_parameter(self):
        stmt = select(SchemaElement.table_name).order_by(
            SchemaElement.embedding.cosine_distance([0.0] * EMBEDDING_DIMENSIONS)
        )
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        assert "<=>" in sql
        assert f"AS TEXT) :: halfvec({EMBEDDING_DIMENSIONS})" in sql


def _fake_client(calls: list[dict]):
    async def embed_content(**kwargs):
        calls.append(kwargs)
        n = len(kwargs["contents"])
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * 4) for _ in range(n)])

    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed_content)))


class TestEmbedTexts:
    def test_batches_requests_and_sets_dimensions(self, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(embeddings, "get_genai_client", lambda: _fake_client(calls))

        texts = [f"doc {i}" for i in range(EMBED_BATCH_SIZE * 2 + 50)]
        vectors = asyncio.run(embeddings.embed_texts(texts, "RETRIEVAL_DOCUMENT"))

        assert len(vectors) == len(texts)
        assert [len(c["contents"]) for c in calls] == [EMBED_BATCH_SIZE, EMBED_BATCH_SIZE, 50]
        assert all(c["model"] == EMBEDDING_MODEL for c in calls)
        assert calls[0]["config"].output_dimensionality == EMBEDDING_DIMENSIONS
        assert calls[0]["config"].task_type == "RETRIEVAL_DOCUMENT"

    def test_query_uses_query_task_type(self, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(embeddings, "get_genai_client", lambda: _fake_client(calls))
        asyncio.run(embeddings.embed_query("top customers"))
        assert calls[0]["config"].task_type == "RETRIEVAL_QUERY"
