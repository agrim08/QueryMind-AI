"""SQLAlchemy ORM models for the QueryMind application database."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ai_config import EMBEDDING_DIMENSIONS
from app.db.session import Base
from app.db.types import HalfVector


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    clerk_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    connections: Mapped[list["DBConnection"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    query_logs: Mapped[list["QueryLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    design_logs: Mapped[list["DesignLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class DBConnection(Base):
    __tablename__ = "db_connections"
    __table_args__ = (Index("ix_db_connections_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_conn_string: Mapped[str] = mapped_column(Text, nullable=False)
    # The legacy `pinecone_namespace` column still exists in the database (nullable,
    # unused since the move to pgvector); a later contract migration drops it.
    table_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set while an indexing run holds this connection; stale claims expire (see schema_indexer).
    indexing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="connections")
    query_logs: Mapped[list["QueryLog"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class QueryLog(Base):
    __tablename__ = "query_logs"
    __table_args__ = (Index("ix_query_logs_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False
    )
    nl_query: Mapped[str] = mapped_column(Text, nullable=False)
    generated_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exec_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="query_logs")
    connection: Mapped["DBConnection"] = relationship(back_populates="query_logs")


class DesignLog(Base):
    __tablename__ = "design_logs"
    __table_args__ = (Index("ix_design_logs_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="design_logs")


# Searchable text for keyword / hybrid retrieval, maintained by Postgres itself.
_SEARCH_TEXT = (
    "to_tsvector('simple'::regconfig, coalesce(schema_name, '') || ' ' || table_name"
    " || ' ' || coalesce(column_name, '') || ' ' || doc)"
)


class SchemaElement(Base):
    """One indexed piece of a connection's schema (a table today; columns later).

    Replaces Pinecone. Rows are deleted automatically with their connection or user,
    and every lookup is scoped by connection_id.
    """

    __tablename__ = "schema_elements"
    __table_args__ = (
        Index("ix_schema_elements_connection_id", "connection_id"),
        Index("ix_schema_elements_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "table" | "column"
    schema_name: Mapped[str] = mapped_column(String(255), nullable=False, default="public")
    table_name: Mapped[str] = mapped_column(String(255), nullable=False)
    column_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    doc: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(HalfVector(EMBEDDING_DIMENSIONS), nullable=False)
    search_tsv: Mapped[str] = mapped_column(TSVECTOR, Computed(_SEARCH_TEXT, persisted=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
