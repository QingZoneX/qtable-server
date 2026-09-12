from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Optional

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services.attachment_storage import (
    cleanup_pending_attachments,
    reconcile_purged_record_attachments,
)

logger = logging.getLogger(__name__)

_cleanup_task: Optional[asyncio.Task] = None


async def _cleanup_once() -> None:
    if not settings.ATTACHMENT_STORAGE_ENABLED:
        return
    async with AsyncSessionLocal() as db:
        marked = await reconcile_purged_record_attachments(db)
        cleaned, pending = await cleanup_pending_attachments(db)
        if marked or cleaned or pending:
            logger.info(
                "Attachment cleanup reconciliation marked=%s cleaned=%s pending=%s",
                marked,
                cleaned,
                pending,
            )


async def _cleanup_loop() -> None:
    while True:
        try:
            await _cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Storage/database outages must not turn a retriable cleanup into an
            # API process crash. Registry rows remain durable and are retried.
            logger.exception("Attachment cleanup pass failed")
        # Keep the documented/configured interval authoritative while protecting
        # the process from a zero/negative value turning an outage into a hot loop.
        interval = max(0.1, float(settings.ATTACHMENT_CLEANUP_INTERVAL_SECONDS))
        await asyncio.sleep(interval)


async def start_attachment_cleanup_worker() -> None:
    global _cleanup_task
    if not settings.ATTACHMENT_STORAGE_ENABLED:
        return
    if _cleanup_task is not None and not _cleanup_task.done():
        return
    _cleanup_task = asyncio.create_task(_cleanup_loop(), name="attachment-cleanup")


async def stop_attachment_cleanup_worker() -> None:
    global _cleanup_task
    if _cleanup_task is None:
        return
    _cleanup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _cleanup_task
    _cleanup_task = None
