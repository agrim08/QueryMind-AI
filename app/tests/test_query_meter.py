"""query_meter against a real, migrated app database (opt-in).

Set QM_TEST_APP_DATABASE_URL to a migrated app database that isn't production, e.g. the eval
one: postgresql+asyncpg://postgres@localhost:55433/qm_evals_app (python -m evals.setup).
Each test creates its own user and connection and deletes them afterwards.
"""
import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.exceptions import Conflict, LimitReached, NotFound
from app.models.models import DBConnection, QueryLog, User
from app.services import query_meter
from app.services.query_pipeline import PipelineOutcome

APP_URL = os.environ.get("QM_TEST_APP_DATABASE_URL")
pytestmark = pytest.mark.skipif(not APP_URL, reason="set QM_TEST_APP_DATABASE_URL to run")


def _run(scenario):
    """Run `scenario(sessions, user_id, connection_id)` with a throwaway user and connection."""

    async def main():
        engine = create_async_engine(APP_URL)
        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with sessions() as s:
            user = User(clerk_id=f"test-meter-{uuid.uuid4()}", email="meter@test.local")
            s.add(user)
            await s.flush()
            connection = DBConnection(user_id=user.id, name="meter-test", encrypted_conn_string="x")
            s.add(connection)
            await s.commit()
        try:
            return await scenario(sessions, user.id, connection.id)
        finally:
            async with sessions() as s:
                await s.execute(delete(User).where(User.id == user.id))  # cascades
                await s.commit()
            await engine.dispose()

    return asyncio.run(main())


async def _reserve(sessions, user_id, connection_id, limit):
    async with sessions() as s:
        return await query_meter.reserve(s, user_id, connection_id, "q", limit)


def test_parallel_questions_cannot_both_get_the_last_slot():
    async def scenario(sessions, user_id, connection_id):
        outcomes = await asyncio.gather(
            *(_reserve(sessions, user_id, connection_id, limit=1) for _ in range(5)),
            return_exceptions=True,
        )
        async with sessions() as s:
            rows = await s.scalar(select(func.count(QueryLog.id)).where(QueryLog.user_id == user_id))
        return outcomes, rows

    outcomes, rows = _run(scenario)
    reserved = [o for o in outcomes if isinstance(o, uuid.UUID)]
    refused = [o for o in outcomes if isinstance(o, (Conflict, LimitReached))]
    assert len(reserved) == 1 and len(refused) == 4
    assert rows == 1


def test_finished_questions_count_toward_the_limit(monkeypatch):
    async def scenario(sessions, user_id, connection_id):
        monkeypatch.setattr(query_meter, "AsyncSessionLocal", sessions)
        log_id = await _reserve(sessions, user_id, connection_id, limit=1)
        await query_meter.finish(log_id, PipelineOutcome(status="success", generated_sql="SELECT 1"))
        with pytest.raises(LimitReached):
            await _reserve(sessions, user_id, connection_id, limit=1)
        assert isinstance(await _reserve(sessions, user_id, connection_id, limit=2), uuid.UUID)

    _run(scenario)


def test_disconnected_question_is_recorded_as_stopped(monkeypatch):
    async def scenario(sessions, user_id, connection_id):
        monkeypatch.setattr(query_meter, "AsyncSessionLocal", sessions)
        log_id = await _reserve(sessions, user_id, connection_id, limit=10)
        await query_meter.finish(log_id, PipelineOutcome())  # still "pending": the client left
        async with sessions() as s:
            return await s.get(QueryLog, log_id)

    log = _run(scenario)
    assert log.status == "error" and log.error_message == "Stopped before finishing."


def test_answering_a_clarification_continues_the_same_question(monkeypatch):
    async def scenario(sessions, user_id, connection_id):
        monkeypatch.setattr(query_meter, "AsyncSessionLocal", sessions)
        log_id = await _reserve(sessions, user_id, connection_id, limit=1)
        await query_meter.finish(log_id, PipelineOutcome(status="clarify", generated_sql="-- Clarify: ?"))
        async with sessions() as s:
            resumed = await query_meter.resume(s, user_id, connection_id, log_id)
        assert resumed.id == log_id and resumed.status == "pending"
        async with sessions() as s:
            rows = await s.scalar(select(func.count(QueryLog.id)).where(QueryLog.user_id == user_id))
        assert rows == 1  # still one question, so a limit of 1 wasn't exceeded
        with pytest.raises(NotFound):  # can't be resumed twice
            async with sessions() as s:
                await query_meter.finish(log_id, PipelineOutcome(status="success"))
                await query_meter.resume(s, user_id, connection_id, log_id)

    _run(scenario)


def test_only_the_owner_can_resume_a_clarification(monkeypatch):
    async def scenario(sessions, user_id, connection_id):
        monkeypatch.setattr(query_meter, "AsyncSessionLocal", sessions)
        log_id = await _reserve(sessions, user_id, connection_id, limit=5)
        await query_meter.finish(log_id, PipelineOutcome(status="clarify"))
        async with sessions() as s:
            other = User(clerk_id=f"test-meter-other-{uuid.uuid4()}", email="o@test.local")
            s.add(other)
            await s.commit()
        try:
            with pytest.raises(NotFound):
                async with sessions() as s:
                    await query_meter.resume(s, other.id, connection_id, log_id)
            with pytest.raises(NotFound):  # right user, wrong connection
                async with sessions() as s:
                    await query_meter.resume(s, user_id, uuid.uuid4(), log_id)
        finally:
            async with sessions() as s:
                await s.execute(delete(User).where(User.id == other.id))
                await s.commit()

    _run(scenario)


def test_abandoned_pending_question_stops_blocking_after_the_window():
    async def scenario(sessions, user_id, connection_id):
        await _reserve(sessions, user_id, connection_id, limit=10)
        with pytest.raises(Conflict):
            await _reserve(sessions, user_id, connection_id, limit=10)
        async with sessions() as s:  # simulate a worker that crashed long ago
            await s.execute(
                update(QueryLog)
                .where(QueryLog.user_id == user_id)
                .values(created_at=func.now() - query_meter.IN_FLIGHT_WINDOW - timedelta(seconds=1))
            )
            await s.commit()
        assert isinstance(await _reserve(sessions, user_id, connection_id, limit=10), uuid.UUID)

    _run(scenario)
