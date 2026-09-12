from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.agent_runtime.types import ActionPreview, ConfirmRequest, ConfirmStatus

logger = logging.getLogger(__name__)


class ConfirmManager:
    def __init__(self) -> None:
        self._pending: dict[str, ConfirmRequest] = {}
        self._timeout_tasks: dict[str, asyncio.Task] = {}

    async def create_confirm(
        self,
        step_id: str,
        preview: ActionPreview,
        timeout_ms: int = 60000,
    ) -> ConfirmRequest:
        confirm = ConfirmRequest(
            confirm_id=str(uuid.uuid4()),
            step_id=step_id,
            preview=preview,
            status=ConfirmStatus.PENDING,
            timeout_ms=timeout_ms,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._pending[confirm.confirm_id] = confirm

        if timeout_ms > 0:
            self._timeout_tasks[confirm.confirm_id] = asyncio.create_task(
                self._auto_timeout(confirm.confirm_id, timeout_ms)
            )

        logger.info(
            "ConfirmManager: created confirm_id=%s step_id=%s timeout_ms=%d",
            confirm.confirm_id,
            step_id,
            timeout_ms,
        )
        return confirm

    async def resolve(
        self,
        confirm_id: str,
        approved: bool,
    ) -> ConfirmRequest:
        confirm = self._pending.pop(confirm_id, None)
        if not confirm:
            raise ValueError(f"Confirm not found: {confirm_id}")

        task = self._timeout_tasks.pop(confirm_id, None)
        if task:
            task.cancel()

        confirm.status = ConfirmStatus.APPROVED if approved else ConfirmStatus.REJECTED
        confirm.resolved_at = datetime.now(timezone.utc).isoformat()

        logger.info(
            "ConfirmManager: resolved confirm_id=%s approved=%s",
            confirm_id,
            approved,
        )
        return confirm

    def get_pending(self, confirm_id: str) -> Optional[ConfirmRequest]:
        return self._pending.get(confirm_id)

    def clear(self) -> None:
        for task in self._timeout_tasks.values():
            task.cancel()
        self._timeout_tasks.clear()
        self._pending.clear()

    async def _auto_timeout(self, confirm_id: str, timeout_ms: int) -> None:
        await asyncio.sleep(timeout_ms / 1000)
        confirm = self._pending.pop(confirm_id, None)
        if confirm and confirm.status == ConfirmStatus.PENDING:
            confirm.status = ConfirmStatus.TIMEOUT
            confirm.resolved_at = datetime.now(timezone.utc).isoformat()
            logger.info("ConfirmManager: timeout confirm_id=%s", confirm_id)


_confirm_manager: ConfirmManager | None = None


def get_confirm_manager() -> ConfirmManager:
    global _confirm_manager
    if _confirm_manager is None:
        _confirm_manager = ConfirmManager()
    return _confirm_manager
