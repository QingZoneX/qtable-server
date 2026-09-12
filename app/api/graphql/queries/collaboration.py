from __future__ import annotations

from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _require_item_permission,
    _require_record_permission,
    _require_user,
    _resolve_backend,
    _resolve_db_table_id,
)
from app.services.collaboration.activity import list_record_activity
from app.services.collaboration.assignment_repair import (
    repair_assignment_notifications_for_user,
)
from app.services.collaboration.comments import list_comments, mention_candidates
from app.services.collaboration.common import CollaborationError
from app.services.collaboration.notifications import (
    list_notifications,
    sync_user_notifications,
    unread_count,
)


async def _repair_and_sync_notifications(
    db: AsyncSession,
    *,
    user_id: int,
    timezone: str,
) -> None:
    """Repair long-offline assignment gaps before the bounded realtime sync."""
    await repair_assignment_notifications_for_user(db, user_id=user_id)
    await sync_user_notifications(
        db,
        user_id=user_id,
        timezone_name=timezone,
    )


@strawberry.type
class CollaborationQueries:
    @strawberry.field(name="recordComments")
    async def record_comments(
        self,
        info: Info,
        table_id: str,
        record_id: strawberry.ID,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> JSON:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Record comments require database backend")
        resolved = _resolve_db_table_id(table_id)
        await _require_record_permission(info, resolved, str(record_id), "read")
        db: AsyncSession = info.context["db"]
        try:
            return await list_comments(
                db,
                table_id=resolved,
                record_id=str(record_id),
                cursor=cursor,
                limit=limit,
            )
        except CollaborationError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="mentionCandidates")
    async def mention_candidates_query(
        self,
        info: Info,
        table_id: str,
    ) -> List[JSON]:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Mentions require database backend")
        resolved = _resolve_db_table_id(table_id)
        await _require_item_permission(info, resolved, "read")
        db: AsyncSession = info.context["db"]
        try:
            return await mention_candidates(db, table_id=resolved)
        except CollaborationError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="notifications")
    async def notifications_query(
        self,
        info: Info,
        cursor: Optional[str] = None,
        limit: int = 30,
        unread_only: bool = False,
        types: Optional[List[str]] = None,
        timezone: str = "UTC",
    ) -> JSON:
        user = await _require_user(info)
        db: AsyncSession = info.context["db"]
        try:
            await _repair_and_sync_notifications(
                db,
                user_id=user.id,
                timezone=timezone,
            )
            return await list_notifications(
                db,
                user_id=user.id,
                cursor=cursor,
                limit=limit,
                unread_only=unread_only,
                types=types,
            )
        except CollaborationError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="notificationUnreadCount")
    async def notification_unread_count(
        self,
        info: Info,
        timezone: str = "UTC",
    ) -> int:
        user = await _require_user(info)
        db: AsyncSession = info.context["db"]
        try:
            await _repair_and_sync_notifications(
                db,
                user_id=user.id,
                timezone=timezone,
            )
            return await unread_count(db, user_id=user.id)
        except CollaborationError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="recordActivity")
    async def record_activity(
        self,
        info: Info,
        table_id: str,
        record_id: strawberry.ID,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> JSON:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Record activity requires database backend")
        resolved = _resolve_db_table_id(table_id)
        await _require_record_permission(info, resolved, str(record_id), "read")
        db: AsyncSession = info.context["db"]
        try:
            return await list_record_activity(
                db,
                table_id=resolved,
                record_id=str(record_id),
                cursor=cursor,
                limit=limit,
            )
        except CollaborationError as exc:
            raise GraphQLError(str(exc)) from exc
