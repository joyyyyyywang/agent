"""Identify resident cleanup tasks that service shutdown must join, not cancel.

Keep this registry independent of native executors and platform-specific imports:
ordinary Web shutdown can consult it even when no external Agent was enabled.
Only cleanup is registered here; model work, readers and resource guards remain
cancellable. The owner still awaits these tasks and observes their failures.
"""

from __future__ import annotations

import asyncio
import weakref


_TASKS: weakref.WeakSet = weakref.WeakSet()


def create_cleanup_task(awaitable) -> asyncio.Task:
    task = asyncio.create_task(awaitable)
    _TASKS.add(task)
    return task


def cleanup_tasks() -> set[asyncio.Task]:
    """Return unfinished cleanup owned by the current event loop only."""
    loop = asyncio.get_running_loop()
    return {task for task in _TASKS if task.get_loop() is loop and not task.done()}
