"""Fire-and-forget tasks that are neither garbage-collected early nor silently failing.

asyncio only keeps weak references to tasks, so an unreferenced task can disappear
mid-run; and an exception in a task nobody awaits is never seen. `spawn` fixes both.
"""
import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_running: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _running.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Background task %s failed", task.get_name(), exc_info=task.exception())
