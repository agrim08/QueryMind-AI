"""Business context and knowledge items per connection (Phase 3).

Every function is scoped by connection and user. Items come from three places: the setup
extraction ("ai"), the user's own edits ("user"), and answers to clarifying questions
("clarification"). Re-extracting replaces only the "ai" items, never the user's.
"""
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.exceptions import InvalidInput, NotFound, RateLimited
from app.models.models import BusinessContext, KnowledgeItem, QueryLog
from app.services.prompt_context import KnowledgeEntry
from app.services.reply_format import parse_reply

KINDS = ("metric", "term", "filter", "convention", "table_note", "clarification")
MAX_ITEMS_PER_CONNECTION = 200
# Setup AI calls (draft + extract) per connection per UTC day. They don't count toward the
# monthly question limit, so this keeps the free Gemini tier safe from repeated clicks.
SETUP_CALLS_PER_DAY = 10


@dataclass(frozen=True)
class ItemDraft:
    kind: str
    name: str
    definition: str


async def get_context(session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID) -> BusinessContext:
    """The connection's business context, created empty on first use."""
    context = await session.scalar(
        select(BusinessContext).where(
            BusinessContext.connection_id == connection_id, BusinessContext.user_id == user_id
        )
    )
    if context is None:
        context = BusinessContext(connection_id=connection_id, user_id=user_id, description="", starter_questions=[])
        session.add(context)
        await session.flush()
    return context


def setup_calls_left(context: BusinessContext, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    used = context.setup_calls_used if context.setup_calls_day == today else 0
    return max(0, SETUP_CALLS_PER_DAY - used)


def use_setup_call(context: BusinessContext, today: date | None = None) -> None:
    """Count one setup AI call, or raise RateLimited when today's are used up."""
    today = today or datetime.now(timezone.utc).date()
    if setup_calls_left(context, today) == 0:
        raise RateLimited(errors.SETUP_CALLS_USED)
    if context.setup_calls_day != today:
        context.setup_calls_day, context.setup_calls_used = today, 0
    context.setup_calls_used += 1


async def list_items(session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID) -> list[KnowledgeItem]:
    result = await session.scalars(
        select(KnowledgeItem)
        .where(KnowledgeItem.connection_id == connection_id, KnowledgeItem.user_id == user_id)
        .order_by(KnowledgeItem.kind, KnowledgeItem.name)
    )
    return list(result)


async def entries(session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID) -> list[KnowledgeEntry]:
    """All items as prompt entries (selection per question happens in prompt_context)."""
    return [KnowledgeEntry(i.kind, i.name, i.definition) for i in await list_items(session, user_id, connection_id)]


async def _count(session: AsyncSession, connection_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count(KnowledgeItem.id)).where(KnowledgeItem.connection_id == connection_id)
    ) or 0


async def add_item(
    session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, draft: ItemDraft, source: str = "user"
) -> KnowledgeItem:
    if draft.kind not in KINDS:
        raise InvalidInput(errors.KNOWLEDGE_KIND_INVALID)
    if await _count(session, connection_id) >= MAX_ITEMS_PER_CONNECTION:
        raise InvalidInput(errors.KNOWLEDGE_FULL)
    item = KnowledgeItem(
        connection_id=connection_id, user_id=user_id, kind=draft.kind,
        name=draft.name, definition=draft.definition, source=source,
    )
    session.add(item)
    await session.flush()
    return item


async def get_item(
    session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, item_id: uuid.UUID
) -> KnowledgeItem:
    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.id == item_id,
            KnowledgeItem.connection_id == connection_id,
            KnowledgeItem.user_id == user_id,
        )
    )
    if item is None:
        raise NotFound(errors.KNOWLEDGE_NOT_FOUND)
    return item


async def replace_ai_items(
    session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, drafts: list[ItemDraft]
) -> None:
    """Swap the AI-extracted items for a new set; the user's own items are kept."""
    await session.execute(
        delete(KnowledgeItem).where(
            KnowledgeItem.connection_id == connection_id,
            KnowledgeItem.user_id == user_id,
            KnowledgeItem.source == "ai",
        )
    )
    room = MAX_ITEMS_PER_CONNECTION - await _count(session, connection_id)
    for draft in [d for d in drafts if d.kind in KINDS][: max(0, room)]:
        session.add(
            KnowledgeItem(
                connection_id=connection_id, user_id=user_id, kind=draft.kind,
                name=draft.name, definition=draft.definition, source="ai",
            )
        )
    await session.flush()


async def remember_clarification(session: AsyncSession, user_id: uuid.UUID, log: QueryLog, answer: str) -> None:
    """Save the user's answer to the clarifying question `log` asked, so a similar question
    isn't asked twice. The item's name is the original question (matched against new
    questions by word overlap). Commits."""
    clarify = parse_reply(log.generated_sql or "").clarify
    if clarify is None or await _count(session, log.connection_id) >= MAX_ITEMS_PER_CONNECTION:
        return  # nothing to remember, or knowledge is full (the answer still applies now)
    session.add(
        KnowledgeItem(
            connection_id=log.connection_id, user_id=user_id, kind="clarification",
            name=log.nl_query[:200], definition=f"{clarify.question} → {answer}"[:1000], source="clarification",
        )
    )
    await session.commit()
