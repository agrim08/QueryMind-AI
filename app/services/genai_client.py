"""One Gemini client per process.

Creating a client per request repeats TLS setup on every question (slower, and
wasteful on the free tier). Callers use `get_genai_client().aio` for async calls.
"""
from functools import lru_cache

from google import genai

from app.core.config import settings


@lru_cache(maxsize=1)
def get_genai_client() -> genai.Client:
    return genai.Client(api_key=settings.GOOGLE_API_KEY)
