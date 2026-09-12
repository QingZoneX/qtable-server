from __future__ import annotations

from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _require_record_permission,
    _require_user,
    _resolve_backend,
    _resolve_db_table_id,
)
from app.models.collaboration import RecordComment
from app.services.collaboration.comments import (
    create_comment,
    delete_comment,
    update_comment,
)
from app.services.collaboration.common import (
    CollaborationError,
    CollaborationNotFoundError,
    table_item,
)
from app.services.collaboration.notifications import (
    mark_all_notifications_read,
    mark_notification_read,
    serialize_notification,
    unread_count,
)


def _graphql_error(exc: Exception) -> GraphQLError:
    return GraphQLError(str(exc))


@strawberry.type
class CollaborationMutations:
    @strawberry.mutation(name="createRecordComment")
    async def create_record_comment(
        self,
        info: Info,
        table_id: str,
        record_id: strawberry.ID,
        body: str,
        mention_user_ids: Optional[List[int]] = None,
        parent_comment_id: Optional[strawberry.ID] = None,
        client_mutation_id: Optional[str] = None,
    ) -> JSON:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Record comments require database backend")
        resolved = _resolve_db_table_id(table_id)
        await _require_record_permission(info, resolved, str(record_id), "update")
        user = await _require_user(info)
        db: AsyncSession = info.context["db"]
        try:
            table = await table_item(db, resolved)
            return await create_comment(
                db,
                workspace_id=str(table.workspace_id),
                table_id=resolved,
                record_id=str(record_id),
                author_id=user.id,
                body=body,
                mention_user_ids=mention_user_ids or [],
                parent_comment_id=(
                    str(parent_comment_id) if parent_comment_id is not None else None
                ),
                client_mutation_id=client_mutation_id,
            )
        except CollaborationError as exc:
            raise _graphql_error(exc) from exc

    @strawberry.mutation(name="updateRecordComment")
    async def update_record_comment(
        self,
        info: Info,
        comment_id: strawberry.ID,
        body: str,
        mention_user_ids: Optional[List[int]] = None,
        expected_revision: Optional[int] = None,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        comment = (
            await db.execute(
                select(RecordComment).where(RecordComment.id == str(comment_id)).limit(1)
            )
        ).scalars().first()
        if comment is None:
            raise GraphQLError("Comment not found")
        if _resolve_backend(comment.table_id) != "db":
            raise GraphQLError("Record comments require database backend")
        permission = await _require_record_permission(
            info,
            comment.table_id,
            comment.record_id,
            "update",
        )
        user = await _require_user(info)
        try:
            return await update_comment(
                db,
                comment_id=comment.id,
                actor_id=user.id,
                actor_permission=permission,
                body=body,
                mention_user_ids=mention_user_ids or [],
                expected_revision=expected_revision,
            )
        except CollaborationError as exc:
            raise _graphql_error(exc) from exc

    @strawberry.mutation(name="deleteRecordComment")
    async def delete_record_comment(
        self,
        info: Info,
        comment_id: strawberry.ID,
        expected_revision: Optional[int] = None,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        comment = (
            await db.execute(
                select(RecordComment).where(RecordComment.id == str(comment_id)).limit(1)
            )
        ).scalars().first()
        if comment is None:
            raise GraphQLError("Comment not found")
        permission = await _require_record_permission(
            info,
            comment.table_id,
            comment.record_id,
            "update",
        )
        user = await _require_user(info)
        try:
            return await delete_comment(
                db,
                comment_id=comment.id,
                actor_id=user.id,
                actor_permission=permission,
                expected_revision=expected_revision,
            )
        except CollaborationError as exc:
            raise _graphql_error(exc) from exc

    @strawberry.mutation(name="markNotificationRead")
    async def mark_notification_read_mutation(
        self,
        info: Info,
        notification_id: strawberry.ID,
    ) -> Optional[JSON]:
        user = await _require_user(info)
        db: AsyncSession = info.context["db"]
        row = await mark_notification_read(
            db,
            user_id=user.id,
            notification_id=str(notification_id),
        )
        if row is None:
            return None
        try:
            return await serialize_notification(
                db,
                notification=row,
                user_id=user.id,
            )
        except CollaborationError as exc:
            raise _graphql_error(exc) from exc

    @strawberry.mutation(name="markAllNotificationsRead")
    async def mark_all_notifications_read_mutation(
        self,
        info: Info,
        types: Optional[List[str]] = None,
    ) -> JSON:
        user = await _require_user(info)
        db: AsyncSession = info.context["db"]
        try:
            count = await mark_all_notifications_read(
                db,
                user_id=user.id,
                types=types,
            )
            return {
                "updatedCount": count,
                "unreadCount": await unread_count(db, user_id=user.id),
            }
        except CollaborationError as exc:
            raise _graphql_error(exc) from exc
