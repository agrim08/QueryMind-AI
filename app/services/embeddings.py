"""Text embeddings for schema documents and questions.

The only module that calls the embedding model, so the model, task types and
dimensions can't drift between indexing and retrieval.
"""
from typing import Literal

from google.genai import types as genai_types

from app.core.ai_config import EMBED_BATCH_SIZE, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from app.services.genai_client import get_genai_client

TaskType = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


async def embed_texts(texts: list[str], task_type: TaskType) -> list[list[float]]:
    """Embed `texts` in batches of EMBED_BATCH_SIZE (one API request per batch)."""
    client = get_genai_client()
    config = genai_types.EmbedContentConfig(
        task_type=task_type, output_dimensionality=EMBEDDING_DIMENSIONS
    )
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        result = await client.aio.models.embed_content(
            model=EMBEDDING_MODEL, contents=batch, config=config
        )
        vectors.extend(list(e.values) for e in result.embeddings)
    if len(vectors) != len(texts):
        raise RuntimeError(f"embedding count mismatch: sent {len(texts)}, got {len(vectors)}")
    return vectors


async def embed_query(text: str) -> list[float]:
    """Embed one question for retrieval."""
    return (await embed_texts([text], "RETRIEVAL_QUERY"))[0]
