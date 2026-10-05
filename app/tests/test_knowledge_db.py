"""Phase 3 services against a real, migrated app database (opt-in, like test_query_meter).

Set QM_TEST_APP_DATABASE_URL to a migrated non-production app database, e.g. the eval one:
postgresql+asyncpg://postgres@localhost:55433/qm_evals_app (python -m evals.setup).
"""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.models import DBConnection, KnowledgeItem, QueryLog, User
from app.services import answer_cache, knowledge, query_meter, verified_queries
from app.services.knowledge import ItemDraft
from app.services.question_context import previous_turns

APP_URL = os.environ.get("QM_TEST_APP_DATABASE_URL")
pytestmark = pytest.mark.skipif(not APP_URL, reason="set QM_TEST_APP_DATABASE_URL to run")


def _run(scenario):
    """Run `scenario(session, user_id, connection_id)` with a throwaway user and connection."""

    async def main():
        engine = create_async_engine(APP_URL)
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with sessions() as s:
            user = User(clerk_id=f"test-knowledge-{uuid.uuid4()}", email="k@test.local")
            s.add(user)
            await s.flush()
            connection = DBConnection(user_id=user.id, name="knowledge-test", encrypted_conn_string="x")
            s.add(connection)
            await s.commit()
        try:
            async with sessions() as s:
                return await scenario(s, user.id, connection.id)
        finally:
            async with sessions() as s:
                await s.execute(delete(User).where(User.id == user.id))  # cascades everything
                await s.commit()
            await engine.dispose()

    return asyncio.run(main())


async def _answered(s, user_id, connection_id, question, sql, follow_up_of=None) -> uuid.UUID:
    log = QueryLog(
        user_id=user_id, connection_id=connection_id, nl_query=question,
        generated_sql=f"-- Intent: number\n{sql}", status="success", follow_up_of=follow_up_of,
    )
    s.add(log)
    await s.commit()
    return log.id


def test_verified_answers_are_found_for_rephrased_questions():
    async def scenario(s, user_id, connection_id):
        log_id = await _answered(s, user_id, connection_id, "total revenue by month", 'SELECT 1 FROM "invoice"')
        verified = await verified_queries.verify(s, user_id, log_id)
        assert verified.sql == 'SELECT 1 FROM "invoice"'  # the header lines aren't stored
        await verified_queries.verify(s, user_id, log_id)  # verifying twice keeps one row
        assert len(await verified_queries.list_for_connection(s, user_id, connection_id)) == 1
        matches = await verified_queries.similar(s, user_id, connection_id, "revenue by month", 3)
        unrelated = await verified_queries.similar(s, user_id, connection_id, "list all artists", 3)
        return matches, unrelated

    matches, unrelated = _run(scenario)
    assert [m.question for m in matches] == ["total revenue by month"]
    assert matches[0].similarity >= verified_queries.MIN_SIMILARITY
    assert unrelated == []


def test_history_marks_only_the_answer_that_was_verified():
    async def scenario(s, user_id, connection_id):
        older = await _answered(s, user_id, connection_id, "total revenue", 'SELECT 1 FROM "invoice"')
        newer = await _answered(s, user_id, connection_id, "total revenue", 'SELECT 2 FROM "invoice"')
        other = await _answered(s, user_id, connection_id, "list artists", 'SELECT 3 FROM "artist"')
        await verified_queries.verify(s, user_id, newer)  # the user verifies the newer answer
        logs = list(await s.scalars(select(QueryLog).where(QueryLog.user_id == user_id)))
        mine = await verified_queries.verified_log_ids(s, user_id, logs)
        someone_else = await verified_queries.verified_log_ids(s, uuid.uuid4(), logs)
        return mine, someone_else, {"older": older, "newer": newer, "other": other}

    mine, someone_else, ids = _run(scenario)
    assert mine == {ids["newer"]}  # same question, different SQL: not verified
    assert someone_else == set()  # scoped by user


def test_conversation_carries_the_last_two_questions_oldest_first():
    async def scenario(s, user_id, connection_id):
        first = await _answered(s, user_id, connection_id, "revenue by month", "SELECT 1")
        second = await _answered(s, user_id, connection_id, "only 2024", "SELECT 2", follow_up_of=first)
        third = await _answered(s, user_id, connection_id, "and by country", "SELECT 3", follow_up_of=second)
        return await previous_turns(s, user_id, connection_id, third)

    turns = _run(scenario)
    assert [t.question for t in turns] == ["only 2024", "and by country"]
    assert [t.sql for t in turns] == ["SELECT 2", "SELECT 3"]


def test_follow_up_links_only_to_the_users_own_questions():
    async def scenario(s, user_id, connection_id):
        own = await _answered(s, user_id, connection_id, "revenue", "SELECT 1")
        kept = await query_meter.reserve(s, user_id, connection_id, "now only 2024", 50, follow_up_of=own)
        await s.execute(update(QueryLog).where(QueryLog.id == kept).values(status="success"))
        await s.commit()
        dropped = await query_meter.reserve(s, user_id, connection_id, "q", 50, follow_up_of=uuid.uuid4())
        rows = {r.id: r.follow_up_of for r in await s.scalars(select(QueryLog).where(QueryLog.id.in_([kept, dropped])))}
        return own, rows[kept], rows[dropped]

    own, kept_link, dropped_link = _run(scenario)
    assert kept_link == own and dropped_link is None


def test_re_extracting_keeps_the_users_own_definitions():
    async def scenario(s, user_id, connection_id):
        await knowledge.add_item(s, user_id, connection_id, ItemDraft("term", "vip", "spent over 1000"))
        await knowledge.replace_ai_items(s, user_id, connection_id, [ItemDraft("metric", "revenue", "SUM(total)")])
        await knowledge.replace_ai_items(s, user_id, connection_id, [ItemDraft("metric", "revenue", "SUM(paid)")])
        await s.commit()
        return [(i.name, i.definition, i.source) for i in await knowledge.list_items(s, user_id, connection_id)]

    assert _run(scenario) == [("revenue", "SUM(paid)", "ai"), ("vip", "spent over 1000", "user")]


def test_a_clarification_answer_is_remembered():
    async def scenario(s, user_id, connection_id):
        log = QueryLog(
            user_id=user_id, connection_id=connection_id, nl_query="who are our best customers?",
            generated_sql="-- Clarify: Best by what?\n-- Option: By total spent\n-- Option: By orders", status="clarify",
        )
        s.add(log)
        await s.commit()
        await knowledge.remember_clarification(s, user_id, log, "By total spent")
        return await s.scalar(select(KnowledgeItem).where(KnowledgeItem.connection_id == connection_id))

    item = _run(scenario)
    assert (item.kind, item.name, item.definition) == (
        "clarification", "who are our best customers?", "Best by what? → By total spent"
    )


def test_earlier_answers_are_reused_newest_first_and_only_the_users_own():
    async def scenario(s, user_id, connection_id):
        for sql, status in [("SELECT 1", "success"), ("SELECT 2", "success"), ("SELECT 3", "error")]:
            s.add(QueryLog(
                user_id=user_id, connection_id=connection_id, nl_query="total sales?",
                generated_sql=sql, status=status, prompt_hash="h1",
            ))
            await s.commit()  # separate transactions, so created_at increases
        return (
            await answer_cache.earlier_reply(s, user_id, connection_id, "h1"),
            await answer_cache.earlier_reply(s, uuid.uuid4(), connection_id, "h1"),
            await answer_cache.earlier_reply(s, user_id, connection_id, "other"),
        )

    newest, someone_else, different_request = _run(scenario)
    assert newest == "SELECT 2"  # the newest *successful* reply
    assert someone_else is None and different_request is None
