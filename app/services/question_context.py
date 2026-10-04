"""Everything a question brings besides the schema, loaded before the pipeline runs:
the connection's knowledge items, verified examples similar to it, and the conversation it
follows up on. All lookups are scoped by user and connection, and need no AI call.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import QueryLog
from app.services import knowledge, verified_queries
from app.services.prompt_context import MAX_EXAMPLES, Example, KnowledgeEntry, Turn
from app.services.reply_format import parse_reply

# How many earlier questions a follow-up carries (CoSQL / SParC-style context, kept short).
MAX_TURNS = 2


@dataclass(frozen=True)
class QuestionContext:
    """Loaded context; the pipeline picks the relevant knowledge once it knows the tables."""

    knowledge: tuple[KnowledgeEntry, ...] = ()
    examples: tuple[Example, ...] = ()
    turns: tuple[Turn, ...] = ()


async def previous_turns(
    session: AsyncSession, user_id: uuid.UUID, connection_id: uuid.UUID, log_id: uuid.UUID | None
) -> list[Turn]:
    """The question `log_id` and the ones it followed up on, oldest first (at most MAX_TURNS)."""
    turns: list[Turn] = []
    seen: set[uuid.UUID] = set()
    while log_id is not None and len(turns) < MAX_TURNS and log_id not in seen:
        seen.add(log_id)
        log = await session.scalar(
            select(QueryLog).where(
                QueryLog.id == log_id, QueryLog.user_id == user_id, QueryLog.connection_id == connection_id
            )
        )
        if log is None:
            break
        turns.append(Turn(log.nl_query, parse_reply(log.generated_sql or "").sql or None))
        log_id = log.follow_up_of
    return list(reversed(turns))


async def load(
    session: AsyncSession,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    question: str,
    follow_up_of: uuid.UUID | None,
) -> QuestionContext:
    return QuestionContext(
        knowledge=tuple(await knowledge.entries(session, user_id, connection_id)),
        examples=tuple(await verified_queries.similar(session, user_id, connection_id, question, MAX_EXAMPLES)),
        turns=tuple(await previous_turns(session, user_id, connection_id, follow_up_of)),
    )
