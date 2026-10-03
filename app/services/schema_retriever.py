"""Schema Retriever — embeds a NL query and fetches top-k relevant table docs from Pinecone."""
import asyncio
from dataclasses import dataclass

from google import genai
from google.genai import types as genai_types
from pinecone import Pinecone

from app.core.config import settings

EMBEDDING_MODEL = "models/gemini-embedding-002"
TOP_K = 6


@dataclass
class TableDoc:
    table_name: str
    doc: str
    score: float


async def retrieve_schema(nl_query: str, namespace: str) -> list[TableDoc]:
    """Embed the NL query and return the top-k most relevant table docs.

    Runs Pinecone query in a thread executor to avoid blocking the event loop.
    """
    client = genai.Client(api_key=settings.GOOGLE_API_KEY)

    # Embed the query
    embed_result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=nl_query,
            config=genai_types.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
        ),
    )
    query_vector = embed_result.embeddings[0].values

    # Query Pinecone
    pc = Pinecone(api_key=settings.PINECONE_API_KEY)
    index = pc.Index(settings.PINECONE_INDEX_NAME)

    results = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: index.query(
            vector=query_vector,
            top_k=TOP_K,
            namespace=namespace,
            include_metadata=True,
        ),
    )

    table_docs: list[TableDoc] = []
    for match in results.get("matches", []):
        meta = match.get("metadata", {})
        table_docs.append(
            TableDoc(
                table_name=meta.get("table_name", ""),
                doc=meta.get("doc", ""),
                score=match.get("score", 0.0),
            )
        )

    return table_docs
