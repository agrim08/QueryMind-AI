"""Knowledge endpoints — a connection's business description, definitions and verified answers.

Routes live under /connections/{connection_id}/knowledge and are scoped to the owner
(OwnedConnection: another user's connection is a 404). The two AI routes (draft, extract) are
rate-limited and capped per connection per day; they don't count toward the question limit.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, OwnedConnection, rate_limited
from app.core import errors
from app.core.exceptions import InvalidInput, UpstreamFailure
from app.db.session import DbSession
from app.schemas.knowledge import (
    DescriptionDraft,
    DescriptionUpdate,
    KnowledgeItemCreate,
    KnowledgeItemResponse,
    KnowledgeItemUpdate,
    KnowledgeResponse,
)
from app.schemas.schemas import VerifiedQueryResponse
from app.services import business_setup, knowledge, verified_queries
from app.services.knowledge import ItemDraft
from app.services.schema_retriever import within_budget
from app.services.schema_store import TableDoc, all_tables

logger = logging.getLogger(__name__)
router = APIRouter()

SETUP_CALLS_PER_MINUTE = 5


async def _response(db: DbSession, user_id: uuid.UUID, connection_id: uuid.UUID) -> KnowledgeResponse:
    context = await knowledge.get_context(db, user_id, connection_id)
    items = await knowledge.list_items(db, user_id, connection_id)
    verified = await verified_queries.list_for_connection(db, user_id, connection_id)
    return KnowledgeResponse(
        description=context.description,
        starter_questions=list(context.starter_questions or []),
        items=[KnowledgeItemResponse.model_validate(i) for i in items],
        verified_queries=[VerifiedQueryResponse.model_validate(v) for v in verified],
        setup_calls_left=knowledge.setup_calls_left(context),
    )


async def _schema(db: DbSession, connection_id: uuid.UUID) -> list[TableDoc]:
    docs = await all_tables(db, connection_id)
    if not docs:
        raise InvalidInput(errors.SCHEMA_NOT_INDEXED)
    return within_budget(docs)


@router.get("/{connection_id}/knowledge", response_model=KnowledgeResponse)
async def get_knowledge(connection: OwnedConnection, user: CurrentUser, db: DbSession) -> KnowledgeResponse:
    response = await _response(db, user.id, connection.id)
    await db.commit()  # persists the context row created on first visit
    return response


@router.put("/{connection_id}/knowledge/description", response_model=KnowledgeResponse)
async def save_description(
    payload: DescriptionUpdate, connection: OwnedConnection, user: CurrentUser, db: DbSession
) -> KnowledgeResponse:
    """Save the business description as written (no AI call)."""
    context = await knowledge.get_context(db, user.id, connection.id)
    context.description = payload.description
    await db.commit()
    return await _response(db, user.id, connection.id)


@router.post(
    "/{connection_id}/knowledge/draft",
    response_model=DescriptionDraft,
    dependencies=[Depends(rate_limited("knowledge_setup", SETUP_CALLS_PER_MINUTE))],
)
async def draft_description(connection: OwnedConnection, user: CurrentUser, db: DbSession) -> DescriptionDraft:
    """An AI-written starting description from the schema, for the user to correct (not saved)."""
    docs = await _schema(db, connection.id)
    context = await knowledge.get_context(db, user.id, connection.id)
    knowledge.use_setup_call(context)
    await db.commit()
    try:
        return DescriptionDraft(description=await business_setup.draft_description(docs))
    except Exception as exc:
        raise _setup_failure(exc, connection.id) from None


@router.post(
    "/{connection_id}/knowledge/extract",
    response_model=KnowledgeResponse,
    dependencies=[Depends(rate_limited("knowledge_setup", SETUP_CALLS_PER_MINUTE))],
)
async def extract_knowledge(connection: OwnedConnection, user: CurrentUser, db: DbSession) -> KnowledgeResponse:
    """Turn the saved description + schema into definitions and starter questions. Replaces the
    previous AI-extracted definitions; the user's own and remembered clarifications stay."""
    docs = await _schema(db, connection.id)
    context = await knowledge.get_context(db, user.id, connection.id)
    knowledge.use_setup_call(context)
    await db.commit()
    try:
        drafts, starters = await business_setup.extract_knowledge(context.description, docs)
    except Exception as exc:
        raise _setup_failure(exc, connection.id) from None
    await knowledge.replace_ai_items(db, user.id, connection.id, drafts)
    context.starter_questions = starters
    await db.commit()
    return await _response(db, user.id, connection.id)


def _setup_failure(exc: Exception, connection_id: uuid.UUID) -> Exception:
    if errors.is_ai_rate_limited(exc):
        logger.warning("Gemini rate limit during knowledge setup (connection %s)", connection_id)
        return UpstreamFailure(errors.AI_BUSY)
    logger.exception("Knowledge setup failed (connection %s)", connection_id)
    return UpstreamFailure(errors.SETUP_FAILED)


@router.post(
    "/{connection_id}/knowledge/items", response_model=KnowledgeItemResponse, status_code=status.HTTP_201_CREATED
)
async def add_item(
    payload: KnowledgeItemCreate, connection: OwnedConnection, user: CurrentUser, db: DbSession
) -> KnowledgeItemResponse:
    item = await knowledge.add_item(db, user.id, connection.id, ItemDraft(payload.kind, payload.name, payload.definition))
    await db.commit()
    await db.refresh(item)
    return KnowledgeItemResponse.model_validate(item)


@router.patch("/{connection_id}/knowledge/items/{item_id}", response_model=KnowledgeItemResponse)
async def update_item(
    item_id: uuid.UUID, payload: KnowledgeItemUpdate, connection: OwnedConnection, user: CurrentUser, db: DbSession
) -> KnowledgeItemResponse:
    """Edit a definition. Edited AI items become the user's, so re-extracting keeps them."""
    item = await knowledge.get_item(db, user.id, connection.id, item_id)
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(item, field, value)
    if item.source == "ai":
        item.source = "user"
    await db.commit()
    await db.refresh(item)
    return KnowledgeItemResponse.model_validate(item)


@router.delete("/{connection_id}/knowledge/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(item_id: uuid.UUID, connection: OwnedConnection, user: CurrentUser, db: DbSession) -> None:
    await db.delete(await knowledge.get_item(db, user.id, connection.id, item_id))
    await db.commit()


@router.delete("/{connection_id}/knowledge/verified/{verified_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_verified(verified_id: uuid.UUID, connection: OwnedConnection, user: CurrentUser, db: DbSession) -> None:
    await verified_queries.remove(db, user.id, connection.id, verified_id)

