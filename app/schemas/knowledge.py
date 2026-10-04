"""Request/response models for business context and knowledge (Phase 3)."""
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.schemas.schemas import VerifiedQueryResponse

ItemKind = Literal["metric", "term", "filter", "convention", "table_note", "clarification"]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
ItemName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ItemDefinition = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class DescriptionUpdate(BaseModel):
    description: Description


class DescriptionDraft(BaseModel):
    description: str


class KnowledgeItemCreate(BaseModel):
    kind: ItemKind
    name: ItemName
    definition: ItemDefinition


class KnowledgeItemUpdate(BaseModel):
    kind: ItemKind | None = None
    name: ItemName | None = None
    definition: ItemDefinition | None = None


class KnowledgeItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: ItemKind
    name: str
    definition: str
    source: Literal["ai", "user", "clarification"]
    updated_at: datetime


class KnowledgeResponse(BaseModel):
    """Everything the Knowledge page shows for one connection."""

    description: str
    starter_questions: list[str]
    items: list[KnowledgeItemResponse]
    verified_queries: list[VerifiedQueryResponse]
    setup_calls_left: int
