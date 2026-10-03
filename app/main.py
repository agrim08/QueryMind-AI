"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.endpoints import auth, connections, query, design
from app.core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # Could add: warm up Pinecone client, validate API keys, etc.
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Text-to-SQL RAG application — ask questions in plain English, get SQL + results.",
    version="1.0.0",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# CORS — in production, replace with your frontend domain via environment variables
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/users", tags=["auth"])
app.include_router(
    connections.router,
    prefix=f"{settings.API_V1_STR}/connections",
    tags=["connections"],
)
app.include_router(query.router, prefix=f"{settings.API_V1_STR}/query", tags=["query"])
app.include_router(design.router, prefix=f"{settings.API_V1_STR}/design", tags=["design"])


@app.get("/", tags=["health"])
async def health_check() -> dict:
    return {"status": "online", "project": settings.PROJECT_NAME}
