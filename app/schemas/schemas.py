"""Pydantic v2 schemas for request/response validation.

Every inbound string is bounded (see .claude/rules/security.md §7).
"""
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


# Trimmed, non-empty strings with an upper bound.
Email = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ConnectionString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


# ── User ──────────────────────────────────────────────────────────────────────

class UserSyncRequest(BaseModel):
    """Profile fields only. The Clerk user id always comes from the verified token."""

    email: Email
    full_name: Annotated[str, StringConstraints(max_length=255)] | None = None
    avatar_url: Annotated[str, StringConstraints(max_length=2048)] | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    clerk_id: str
    email: str
    full_name: str | None = None
    avatar_url: str | None = None
    created_at: datetime


# ── DB Connection ─────────────────────────────────────────────────────────────

class DBConnectionCreate(BaseModel):
    name: Name
    connection_string: ConnectionString  # raw — normalised and encrypted before storage


class ConnectionTestRequest(BaseModel):
    conn_string: ConnectionString


class ConnectionTestResponse(BaseModel):
    ok: bool
    error: str | None = None


class DBConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    table_count: int | None = None
    indexed_at: datetime | None = None
    is_active: bool
    created_at: datetime


# ── Query ─────────────────────────────────────────────────────────────────────

class ClarificationAnswer(BaseModel):
    """The user's answer to a clarifying question (the `clarify` SSE event)."""

    question_id: uuid.UUID  # from the clarify event; the question's QueryLog id
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class QueryRequest(BaseModel):
    connection_id: uuid.UUID
    nl_query: Question
    # Set when answering a clarifying question; the question then continues (one count).
    clarification: ClarificationAnswer | None = None


# ── Query Log ─────────────────────────────────────────────────────────────────

class QueryLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    nl_query: str
    generated_sql: str | None = None
    row_count: int | None = None
    exec_time_ms: int | None = None
    status: str
    error_message: str | None = None
    created_at: datetime


# ── Usage ─────────────────────────────────────────────────────────────────────

class UsageResponse(BaseModel):
    used: int
    limit: int
    unlimited: bool
