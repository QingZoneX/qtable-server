from __future__ import annotations

import uuid
from typing import Any, Dict, Mapping, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collaboration import RecordComment, RecordCommentMention, UserNotification
from app.models.user import User
from app.services.change_history import append_change_set
from app.services.collaboration.common import (
    CollaborationConflictError,
    CollaborationError,
    CollaborationNotFoundError,
    actor_map,
    actor_payload,
    decode_offset_cursor,
    normalize_user_ids,
    page_info,
    safe_limit,
    sanitize_markdown,
    table_item,
    utcnow,
    validate_workspace_members,
)
from app.services.collaboration.notifications import (
    publish_notification_rows,
    stage_notification,
)
from app.services.workspace import permission_allows


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


async def _comment(db: AsyncSession, comment_id: str) -> Optional[RecordComment]:
    return (
        await db.execute(
            select(RecordComment).where(RecordComment.id == str(comment_id)).limit(1)
        )
    ).scalars().first()


async def _mention_ids(db: AsyncSession, comment_id: str) -> set[int]:
    result = await db.execute(
        select(RecordCommentMention.user_id).where(
            RecordCommentMention.comment_id == str(comment_id)
        )
    )
    return {int(value) for value in result.scalars().all()}


async def _set_mentions(
    db: AsyncSession,
    *,
    comment: RecordComment,
    workspace_id: str,
    mention_user_ids: Sequence[Any],
) -> tuple[set[int], set[int]]:
    normalized = set(normalize_user_ids(mention_user_ids))
    await validate_workspace_members(db, workspace_id, sorted(normalized))
    existing = await _mention_ids(db, comment.id)
    removed = existing - normalized
    added = normalized - existing
    if removed:
        await db.execute(
            delete(RecordCommentMention).where(
                RecordCommentMention.comment_id == comment.id,
                RecordCommentMention.user_id.in_(list(removed)),
            )
        )
    for user_id in sorted(added):
        db.add(
            RecordCommentMention(
                id=_new_id("men"),
                comment_id=comment.id,
                user_id=user_id,
                created_at=utcnow(),
            )
        )
    await db.flush()
    return added, removed


async def _append_comment_history(
    db: AsyncSession,
    *,
    comment: RecordComment,
    actor_id: int,
    operation: str,
    before: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
) -> str:
    change_set = await append_change_set(
        db,
        table_id=comment.table_id,
        actor_id=actor_id,
        actor_type="user",
        operation=operation,
        source="collaboration",
        summary={
            "comment.create": "Create comment",
            "comment.update": "Edit comment",
            "comment.delete": "Delete comment",
        }.get(operation, "Comment changed"),
        items=[
            {
                "table_id": comment.table_id,
                "entity_type": "comment",
                "entity_id": comment.id,
                "before_data": dict(before) if before is not None else None,
                "after_data": dict(after) if after is not None else None,
                "changed_fields": ["body"],
            }
        ],
    )
    return change_set.id


def _history_payload(comment: RecordComment) -> Dict[str, Any]:
    return {
        "recordId": comment.record_id,
        "commentId": comment.id,
        "parentCommentId": comment.parent_comment_id,
        "revision": int(comment.revision or 1),
        "deleted": comment.deleted_at is not None,
    }


async def create_comment(
    db: AsyncSession,
    *,
    workspace_id: str,
    table_id: str,
    record_id: str,
    author_id: int,
    body: str,
    mention_user_ids: Optional[Sequence[Any]] = None,
    parent_comment_id: Optional[str] = None,
    client_mutation_id: Optional[str] = None,
) -> Dict[str, Any]:
    sanitized = sanitize_markdown(body)
    if client_mutation_id:
        existing = (
            await db.execute(
                select(RecordComment).where(
                    RecordComment.author_id == int(author_id),
                    RecordComment.client_mutation_id == str(client_mutation_id),
                ).limit(1)
            )
        ).scalars().first()
        if existing is not None:
            if (
                existing.table_id != table_id
                or existing.record_id != record_id
                or existing.parent_comment_id != parent_comment_id
                or existing.body != sanitized
            ):
                raise CollaborationConflictError(
                    "clientMutationId was already used for another comment payload"
                )
            return await serialize_comment(db, existing)

    parent: Optional[RecordComment] = None
    if parent_comment_id:
        parent = await _comment(db, str(parent_comment_id))
        if (
            parent is None
            or parent.table_id != table_id
            or parent.record_id != record_id
            or parent.deleted_at is not None
        ):
            raise CollaborationNotFoundError("Parent comment not found")
        if parent.parent_comment_id is not None:
            raise CollaborationError("Replies may be nested only one level")

    comment = RecordComment(
        id=_new_id("cmt"),
        workspace_id=workspace_id,
        table_id=table_id,
        record_id=record_id,
        author_id=author_id,
        parent_comment_id=parent.id if parent else None,
        body=sanitized,
        body_format="markdown",
        client_mutation_id=(str(client_mutation_id)[:128] if client_mutation_id else None),
        revision=1,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(comment)
    await db.flush()
    added_mentions, _ = await _set_mentions(
        db,
        comment=comment,
        workspace_id=workspace_id,
        mention_user_ids=mention_user_ids or [],
    )
    change_set_id = await _append_comment_history(
        db,
        comment=comment,
        actor_id=author_id,
        operation="comment.create",
        before=None,
        after=_history_payload(comment),
    )

    created_notifications: list[UserNotification] = []
    for recipient_id in sorted(added_mentions):
        if recipient_id == author_id:
            continue
        notification, is_new = await stage_notification(
            db,
            recipient_user_id=recipient_id,
            type="comment_mention",
            actor_id=author_id,
            workspace_id=workspace_id,
            table_id=table_id,
            record_id=record_id,
            comment_id=comment.id,
            event_id=change_set_id,
            dedupe_key=f"comment:{comment.id}:mention:{recipient_id}",
        )
        if is_new:
            created_notifications.append(notification)

    if (
        parent is not None
        and parent.author_id is not None
        and int(parent.author_id) != int(author_id)
        and int(parent.author_id) not in added_mentions
    ):
        notification, is_new = await stage_notification(
            db,
            recipient_user_id=int(parent.author_id),
            type="comment_reply",
            actor_id=author_id,
            workspace_id=workspace_id,
            table_id=table_id,
            record_id=record_id,
            comment_id=comment.id,
            event_id=change_set_id,
            dedupe_key=f"comment:{comment.id}:reply:{parent.author_id}",
        )
        if is_new:
            created_notifications.append(notification)

    await db.commit()
    await publish_notification_rows(created_notifications)
    return await serialize_comment(db, comment)


async def update_comment(
    db: AsyncSession,
    *,
    comment_id: str,
    actor_id: int,
    actor_permission: str,
    body: str,
    mention_user_ids: Optional[Sequence[Any]] = None,
    expected_revision: Optional[int] = None,
) -> Dict[str, Any]:
    comment = await _comment(db, comment_id)
    if comment is None or comment.deleted_at is not None:
        raise CollaborationNotFoundError("Comment not found")
    if comment.author_id != actor_id and not permission_allows(actor_permission, "manage"):
        raise CollaborationError("Only the author or a manager can edit this comment")
    if expected_revision is not None and int(comment.revision or 1) != int(expected_revision):
        raise CollaborationConflictError("Comment has newer changes")

    before = _history_payload(comment)
    sanitized = sanitize_markdown(body)
    comment.body = sanitized
    comment.revision = int(comment.revision or 1) + 1
    comment.updated_at = utcnow()
    added_mentions, _ = await _set_mentions(
        db,
        comment=comment,
        workspace_id=comment.workspace_id,
        mention_user_ids=mention_user_ids or [],
    )
    after = _history_payload(comment)
    change_set_id = await _append_comment_history(
        db,
        comment=comment,
        actor_id=actor_id,
        operation="comment.update",
        before=before,
        after=after,
    )
    created_notifications: list[UserNotification] = []
    for recipient_id in sorted(added_mentions):
        if recipient_id == actor_id:
            continue
        notification, is_new = await stage_notification(
            db,
            recipient_user_id=recipient_id,
            type="comment_mention",
            actor_id=actor_id,
            workspace_id=comment.workspace_id,
            table_id=comment.table_id,
            record_id=comment.record_id,
            comment_id=comment.id,
            event_id=change_set_id,
            dedupe_key=f"comment:{comment.id}:mention:{recipient_id}",
        )
        if is_new:
            created_notifications.append(notification)
    await db.commit()
    await publish_notification_rows(created_notifications)
    return await serialize_comment(db, comment)


async def delete_comment(
    db: AsyncSession,
    *,
    comment_id: str,
    actor_id: int,
    actor_permission: str,
    expected_revision: Optional[int] = None,
) -> Dict[str, Any]:
    comment = await _comment(db, comment_id)
    if comment is None or comment.deleted_at is not None:
        raise CollaborationNotFoundError("Comment not found")
    if comment.author_id != actor_id and not permission_allows(actor_permission, "manage"):
        raise CollaborationError("Only the author or a manager can delete this comment")
    if expected_revision is not None and int(comment.revision or 1) != int(expected_revision):
        raise CollaborationConflictError("Comment has newer changes")

    before = _history_payload(comment)
    comment.body = ""
    comment.deleted_at = utcnow()
    comment.updated_at = comment.deleted_at
    comment.revision = int(comment.revision or 1) + 1
    await db.execute(
        delete(RecordCommentMention).where(RecordCommentMention.comment_id == comment.id)
    )
    await _append_comment_history(
        db,
        comment=comment,
        actor_id=actor_id,
        operation="comment.delete",
        before=before,
        after=_history_payload(comment),
    )
    await db.commit()
    return await serialize_comment(db, comment)


async def serialize_comment(db: AsyncSession, comment: RecordComment) -> Dict[str, Any]:
    mention_ids = sorted(await _mention_ids(db, comment.id))
    users = await actor_map(db, [comment.author_id, *mention_ids])
    author = users.get(int(comment.author_id)) if comment.author_id is not None else None
    return {
        "id": comment.id,
        "workspaceId": comment.workspace_id,
        "tableId": comment.table_id,
        "recordId": comment.record_id,
        "parentCommentId": comment.parent_comment_id,
        "author": actor_payload(author, comment.author_id),
        "body": None if comment.deleted_at is not None else comment.body,
        "bodyFormat": comment.body_format,
        "mentions": [
            actor_payload(users.get(user_id), user_id)
            for user_id in mention_ids
        ],
        "revision": int(comment.revision or 1),
        "deleted": comment.deleted_at is not None,
        "createdAt": comment.created_at.isoformat() if comment.created_at else None,
        "updatedAt": comment.updated_at.isoformat() if comment.updated_at else None,
        "deletedAt": comment.deleted_at.isoformat() if comment.deleted_at else None,
    }


async def list_comments(
    db: AsyncSession,
    *,
    table_id: str,
    record_id: str,
    cursor: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    safe = safe_limit(limit)
    offset = decode_offset_cursor(cursor)
    conditions = (
        RecordComment.table_id == table_id,
        RecordComment.record_id == str(record_id),
    )
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(RecordComment).where(*conditions)
            )
        ).scalar()
        or 0
    )
    result = await db.execute(
        select(RecordComment)
        .where(*conditions)
        .order_by(RecordComment.created_at.asc(), RecordComment.id.asc())
        .offset(offset)
        .limit(safe)
    )
    rows = list(result.scalars().all())
    return {
        "items": [await serialize_comment(db, row) for row in rows],
        "totalCount": total,
        "pageInfo": page_info(offset, len(rows), total),
    }


async def mention_candidates(db: AsyncSession, *, table_id: str) -> list[Dict[str, Any]]:
    item = await table_item(db, table_id)
    users = await validate_workspace_members(db, item.workspace_id, [])
    # Empty validation intentionally does not enumerate. Query the current
    # member set directly so removed users disappear immediately.
    from app.services.collaboration.common import workspace_members_by_id

    users = await workspace_members_by_id(db, item.workspace_id)
    return [actor_payload(users[user_id], user_id) for user_id in sorted(users)]
