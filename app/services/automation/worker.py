from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Optional

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services.automation.runtime import process_automation_cycle


logger = logging.getLogger(__name__)
_worker_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None


async def _worker_loop(stop_event: asyncio.Event) -> None:
    poll_seconds = max(1.0, float(settings.AUTOMATION_POLL_SECONDS))
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                await process_automation_cycle(
                    db,
                    event_limit=settings.AUTOMATION_EVENT_BATCH_SIZE,
                    retry_limit=settings.AUTOMATION_RETRY_BATCH_SIZE,
                    schedule_limit=settings.AUTOMATION_SCHEDULE_BATCH_SIZE,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automation worker cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass


async def start_automation_worker() -> Optional[asyncio.Task]:
    global _worker_task, _stop_event
    if not settings.AUTOMATION_WORKER_ENABLED:
        return None
    if _worker_task is not None and not _worker_task.done():
        return _worker_task
    _stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_worker_loop(_stop_event), name="qtable-automation-worker")
    return _worker_task


async def stop_automation_worker() -> None:
    global _worker_task, _stop_event
    if _worker_task is None:
        return
    if _stop_event is not None:
        _stop_event.set()
    try:
        await asyncio.wait_for(_worker_task, timeout=max(2.0, float(settings.AUTOMATION_POLL_SECONDS) + 1.0))
    except asyncio.TimeoutError:
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task
    finally:
        _worker_task = None
        _stop_event = None
