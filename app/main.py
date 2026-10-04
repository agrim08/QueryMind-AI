"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.endpoints import auth, connections, design, knowledge, query
from app.core.config import settings
from app.core.exceptions import DomainError
from app.db.session import engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail fast on missing production secrets; release the DB pool on shutdown."""
    if settings.ENVIRONMENT == "production" and (missing := settings.missing_required()):
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Text-to-SQL RAG application — ask questions in plain English, get SQL + results.",
    version="1.0.0",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    """Services raise DomainError subclasses; their message is always user-safe."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """One readable sentence instead of Pydantic's error list (the UI shows `detail` as text)."""
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", ())[1:]) or "request"
    return JSONResponse(
        status_code=422,
        content={"detail": f"Invalid {field}: {first.get('msg', 'check the input and try again')}."},
    )


app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/users", tags=["auth"])
app.include_router(connections.router, prefix=f"{settings.API_V1_STR}/connections", tags=["connections"])
app.include_router(knowledge.router, prefix=f"{settings.API_V1_STR}/connections", tags=["knowledge"])
app.include_router(query.router, prefix=f"{settings.API_V1_STR}/query", tags=["query"])
app.include_router(design.router, prefix=f"{settings.API_V1_STR}/design", tags=["design"])


@app.get("/", tags=["health"])
async def health_check() -> dict:
    return {"status": "online", "project": settings.PROJECT_NAME}
