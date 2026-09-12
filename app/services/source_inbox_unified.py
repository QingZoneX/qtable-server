"""Source Inbox adapter backed by the shared task duplicate detector.

The base SourceInboxService owns ingestion, AI suggestion, conversion and status
transitions.  This adapter replaces only its duplicate-candidate hook so the AI
prompt and the returned preview use the exact same permission-safe detector as
manual creation and TaskPlanning.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_inbox import SourceInboxItem
from app.services.source_inbox import SourceInboxService
from app.services.task_duplicates import task_duplicate_service


class UnifiedSourceInboxService(SourceInboxService):
    async def _visible_task_context(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        table_id: str,
        permission: str,
        item: SourceInboxItem,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        # SourceInboxService has already resolved this target from its editable
        # table list. Keep an explicit edit gate here so this hook stays safe if
        # it is reused independently in the future.
        visible = await task_duplicate_service._visible_store(
            db,
            user_id=user_id,
            table_id=table_id,
            required_permission="edit",
        )
        prepared = task_duplicate_service._prepare_store(visible)
        result = task_duplicate_service._scan_prepared(
            prepared,
            table_id=table_id,
            title=str(
                item.page_title
                or item.annotation
                or item.quote
                or item.canonical_url
                or item.url
                or ""
            ),
            description=str(item.annotation or ""),
            source_quote=str(item.quote or ""),
            source_url=str(item.canonical_url or item.url or ""),
            source_id=str(item.source_id or ""),
            tags=list(item.tags or []),
        )
        fields = [
            dict(field)
            for field in visible.get("fields", [])
            if isinstance(field, dict)
        ]
        return fields, result["candidates"], bool(result["scanTruncated"])


source_inbox_service = UnifiedSourceInboxService()
