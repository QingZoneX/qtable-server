from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.models.change_history import ChangeItem, ChangeSet
from app.models.collaboration import RecordComment, UserNotification
from app.models.smart_table import WorkspaceItem
from app.models.task_profile import TableTaskProfile
from app.services.collaboration.broker import notification_broker
from app.services.collaboration.common import (
    CollaborationError,
    actor_map,
    actor_payload,
    decode_offset_cursor,
    page_info,
    record_deep_link,
    record_title,
    record_visibility,
    safe_limit,
    table_item,
    utcnow,
    workspace_members_by_id,
)
from app.services.my_work import MyWorkValidationError, build_my_work
from app.services.workspace import get_effective_permission_for_item, permission_allows


NOTIFICATION_TYPES = {
    "comment_mention",
    "comment_reply",
    "task_assigned",
    "due_3d",
    "due_24h",
    "ai_action_required",
    "automation",
    "automation_failed",
}
ASSIGNMENT_SCAN_LIMIT = 500
DUE_PAGE_SIZE = 100
DUE_PAGE_CAP = 100


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _payload(value: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not value:
        return None
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))


def _payload_dict(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _member_ids(value: Any) -> set[int]:
    result: set[int] = set()
    if isinstance(value, list):
        for item in value:
            result.update(_member_ids(item))
        return result
    if isinstance(value, Mapping):
        for key in ("id", "userId", "user_id", "value"):
            candidate = value.get(key)
            if candidate is not None:
                try:
                    result.add(int(candidate))
                except (TypeError, ValueError):
                    pass
                break
        return result
    if value is None or isinstance(value, bool):
        return result
    try:
        result.add(int(value))
    except (TypeError, ValueError):
        pass
    return result


async def stage_notification(
    db: AsyncSession,
    *,
    recipient_user_id: int,
    type: str,
    actor_id: Optional[int],
    workspace_id: Optional[str],
    table_id: Optional[str],
    record_id: Optional[str],
    comment_id: Optional[str],
    event_id: str,
    dedupe_key: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> tuple[UserNotification, bool]:
    normalized_type = str(type or "").strip()
    if normalized_type not in NOTIFICATION_TYPES:
        raise CollaborationError(f"Unsupported notification type: {normalized_type}")
    existing = (
        await db.execute(
            select(UserNotification).where(
                UserNotification.recipient_user_id == int(recipient_user_id),
                UserNotification.dedupe_key == str(dedupe_key),
            ).limit(1)
        )
    ).scalars().first()
    if existing is not None:
        return existing, False

    notification = UserNotification(
        id=_new_id("ntf"),
        recipient_user_id=int(recipient_user_id),
        type=normalized_type,
        actor_id=actor_id,
        workspace_id=workspace_id,
        table_id=table_id,
        record_id=record_id,
        comment_id=comment_id,
        event_id=str(event_id)[:128],
        dedupe_key=str(dedupe_key)[:191],
        payload=_payload(payload),
        created_at=utcnow(),
    )
    try:
        async with db.begin_nested():
            db.add(notification)
            await db.flush()
        return notification, True
    except IntegrityError:
        existing = (
            await db.execute(
                select(UserNotification).where(
                    UserNotification.recipient_user_id == int(recipient_user_id),
                    UserNotification.dedupe_key == str(dedupe_key),
                ).limit(1)
            )
        ).scalars().first()
        if existing is None:
            raise
        return existing, False


async def publish_notification_rows(
    rows: Iterable[UserNotification],
    *,
    kind: str = "created",
) -> None:
    for row in rows:
        await notification_broker.publish(
            {
                "kind": kind,
                "recipientUserId": int(row.recipient_user_id),
                "notificationId": row.id,
                "updatedAt": utcnow().isoformat(),
            }
        )


async def unread_count(db: AsyncSession, *, user_id: int) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(UserNotification)
                .where(
                    UserNotification.recipient_user_id == int(user_id),
                    UserNotification.read_at.is_(None),
                )
            )
        ).scalar()
        or 0
    )


async def serialize_notification(
    db: AsyncSession,
    *,
    notification: UserNotification,
    user_id: int,
) -> Dict[str, Any]:
    if int(notification.recipient_user_id) != int(user_id):
        raise CollaborationError("Notification does not belong to current user")

    actors = await actor_map(db, [notification.actor_id])
    actor = (
        actors.get(int(notification.actor_id))
        if notification.actor_id is not None
        else None
    )
    base = {
        "id": notification.id,
        "type": notification.type,
        "actor": actor_payload(actor, notification.actor_id),
        "workspaceId": notification.workspace_id,
        "tableId": notification.table_id,
        "recordId": notification.record_id,
        "commentId": notification.comment_id,
        "createdAt": notification.created_at.isoformat() if notification.created_at else None,
        "readAt": notification.read_at.isoformat() if notification.read_at else None,
        "accessible": True,
        "title": "通知",
        "summary": "",
        "deepLink": None,
        "payload": {},
    }

    if notification.table_id and notification.record_id:
        visible, item, record, _ = await record_visibility(
            db,
            user_id=user_id,
            table_id=str(notification.table_id),
            record_id=str(notification.record_id),
        )
        if not visible or item is None or record is None:
            base.update(
                {
                    "accessible": False,
                    "title": "内容不可访问",
                    "summary": "该内容已删除或你已无权访问。",
                    "deepLink": None,
                    "payload": {},
                }
            )
            return base

        base["title"] = await record_title(
            db,
            table_id=str(notification.table_id),
            record=record,
        )
        base["deepLink"] = await record_deep_link(
            db,
            table=item,
            record_id=str(notification.record_id),
            comment_id=notification.comment_id,
        )
        payload = _payload_dict(notification.payload)
        comment: Optional[RecordComment] = None
        if notification.comment_id:
            comment = (
                await db.execute(
                    select(RecordComment).where(
                        RecordComment.id == str(notification.comment_id),
                        RecordComment.table_id == str(notification.table_id),
                        RecordComment.record_id == str(notification.record_id),
                    ).limit(1)
                )
            ).scalars().first()

        actor_name = base["actor"]["name"]
        if notification.type == "comment_mention":
            if comment is None or comment.deleted_at is not None:
                base["summary"] = f"{actor_name} 在一条已删除的评论中提到了你"
            else:
                preview = str(comment.body or "").replace("\n", " ")[:120]
                base["summary"] = f"{actor_name} 在评论中提到了你：{preview}"
        elif notification.type == "comment_reply":
            if comment is None or comment.deleted_at is not None:
                base["summary"] = f"{actor_name} 的回复已被删除"
            else:
                preview = str(comment.body or "").replace("\n", " ")[:120]
                base["summary"] = f"{actor_name} 回复了你的评论：{preview}"
        elif notification.type == "task_assigned":
            base["summary"] = f"{actor_name} 将任务分配给了你"
        elif notification.type == "due_24h":
            base["summary"] = "任务将在 24 小时内到期"
        elif notification.type == "due_3d":
            base["summary"] = "任务将在 3 天内到期"
        elif notification.type == "ai_action_required":
            base["summary"] = "有一项 AI 操作等待确认"
        elif notification.type == "automation":
            base["summary"] = str(payload.get("message") or "自动化规则已触发")[:500]
        elif notification.type == "automation_failed":
            base["summary"] = "一条自动化规则执行失败"
        else:
            base["summary"] = "有新的协作动态"
        base["payload"] = payload
        return base

    payload = _payload_dict(notification.payload)
    if notification.type == "automation":
        base["summary"] = str(payload.get("message") or "自动化规则已触发")[:500]
    elif notification.type == "automation_failed":
        base["summary"] = "一条自动化规则执行失败"
    else:
        base["summary"] = "有新的协作动态"
    base["payload"] = payload
    return base


async def list_notifications(
    db: AsyncSession,
    *,
    user_id: int,
    cursor: Optional[str] = None,
    limit: int = 30,
    unread_only: bool = False,
    types: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    safe = safe_limit(limit)
    offset = decode_offset_cursor(cursor)
    normalized_types = [
        str(value) for value in (types or []) if str(value) in NOTIFICATION_TYPES
    ]
    conditions = [UserNotification.recipient_user_id == int(user_id)]
    if unread_only:
        conditions.append(UserNotification.read_at.is_(None))
    if normalized_types:
        conditions.append(UserNotification.type.in_(normalized_types))
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(UserNotification).where(*conditions)
            )
        ).scalar()
        or 0
    )
    result = await db.execute(
        select(UserNotification)
        .where(*conditions)
        .order_by(UserNotification.created_at.desc(), UserNotification.id.desc())
        .offset(offset)
        .limit(safe)
    )
    rows = list(result.scalars().all())
    items = [
        await serialize_notification(db, notification=row, user_id=user_id)
        for row in rows
    ]
    return {
        "items": items,
        "totalCount": total,
        "unreadCount": await unread_count(db, user_id=user_id),
        "pageInfo": page_info(offset, len(items), total),
    }


async def mark_notification_read(
    db: AsyncSession,
    *,
    user_id: int,
    notification_id: str,
) -> Optional[UserNotification]:
    row = (
        await db.execute(
            select(UserNotification).where(
                UserNotification.id == str(notification_id),
                UserNotification.recipient_user_id == int(user_id),
            ).limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    if row.read_at is None:
        row.read_at = utcnow()
        await db.commit()
        await publish_notification_rows([row], kind="read")
    return row


async def mark_all_notifications_read(
    db: AsyncSession,
    *,
    user_id: int,
    types: Optional[Sequence[str]] = None,
) -> int:
    normalized_types = [
        str(value) for value in (types or []) if str(value) in NOTIFICATION_TYPES
    ]
    conditions = [
        UserNotification.recipient_user_id == int(user_id),
        UserNotification.read_at.is_(None),
    ]
    if normalized_types:
        conditions.append(UserNotification.type.in_(normalized_types))
    result = await db.execute(select(UserNotification).where(*conditions))
    rows = list(result.scalars().all())
    if not rows:
        return 0
    now = utcnow()
    for row in rows:
        row.read_at = now
    await db.commit()
    await notification_broker.publish(
        {
            "kind": "read_all",
            "recipientUserId": int(user_id),
            "notificationId": None,
            "updatedAt": now.isoformat(),
        }
    )
    return len(rows)


async def sync_assignment_notifications_for_table(
    db: AsyncSession,
    *,
    table_id: str,
) -> list[UserNotification]:
    """Materialize assignment notifications from shared ChangeSet diffs."""
    profile = (
        await db.execute(
            select(TableTaskProfile).where(
                TableTaskProfile.table_id == table_id
            ).limit(1)
        )
    ).scalars().first()
    if profile is None or not isinstance(profile.config, Mapping):
        return []
    assignee_field = profile.config.get("assigneeFieldId")
    if not assignee_field:
        return []
    item = await table_item(db, table_id)
    result = await db.execute(
        select(ChangeItem, ChangeSet)
        .join(ChangeSet, ChangeSet.id == ChangeItem.change_set_id)
        .where(
            ChangeItem.table_id == table_id,
            ChangeItem.entity_type == "record",
            ChangeSet.status == "applied",
        )
        .order_by(ChangeSet.created_at.desc(), ChangeItem.id.desc())
        .limit(ASSIGNMENT_SCAN_LIMIT)
    )
    candidates = list(result.all())
    workspace_members = await workspace_members_by_id(db, str(item.workspace_id))
    created: list[UserNotification] = []
    for change_item, change_set in candidates:
        changed = {str(value) for value in (change_item.changed_fields or [])}
        if str(assignee_field) not in changed:
            continue
        before = change_item.before_data if isinstance(change_item.before_data, Mapping) else {}
        after = change_item.after_data if isinstance(change_item.after_data, Mapping) else {}
        before_ids = _member_ids(before.get(str(assignee_field)))
        after_ids = _member_ids(after.get(str(assignee_field)))
        for recipient_id in sorted(after_ids - before_ids):
            if recipient_id == change_set.actor_id or recipient_id not in workspace_members:
                continue
            notification, is_new = await stage_notification(
                db,
                recipient_user_id=recipient_id,
                type="task_assigned",
                actor_id=change_set.actor_id,
                workspace_id=str(item.workspace_id),
                table_id=table_id,
                record_id=str(change_item.entity_id),
                comment_id=None,
                event_id=change_set.id,
                dedupe_key=f"assignment:{change_set.id}:{change_item.entity_id}:{recipient_id}",
                payload={"source": change_set.source or "record_change"},
            )
            if is_new:
                created.append(notification)
    if created:
        await db.commit()
        await publish_notification_rows(created)
    return created


async def sync_assignment_notifications_for_user(
    db: AsyncSession,
    *,
    user_id: int,
) -> int:
    tables = list(
        (
            await db.execute(
                select(WorkspaceItem)
                .join(TableTaskProfile, TableTaskProfile.table_id == WorkspaceItem.id)
                .where(WorkspaceItem.type == "table")
                .order_by(WorkspaceItem.id.asc())
            )
        ).scalars().all()
    )
    created = 0
    for table in tables:
        permission = await get_effective_permission_for_item(db, user_id, str(table.id))
        if not permission_allows(permission, "read"):
            continue
        rows = await sync_assignment_notifications_for_table(db, table_id=str(table.id))
        created += sum(1 for row in rows if int(row.recipient_user_id) == int(user_id))
    return created


async def _due_candidate_pages(
    db: AsyncSession,
    *,
    user_id: int,
    timezone_name: str,
) -> list[Dict[str, Any]]:
    cursor: Optional[str] = None
    output: list[Dict[str, Any]] = []
    seen_24h: set[tuple[str, str]] = set()
    seen_events: set[tuple[str, str, str]] = set()
    for _ in range(DUE_PAGE_CAP):
        payload = await build_my_work(
            db,
            user_id=user_id,
            sections=["due"],
            limit=DUE_PAGE_SIZE,
            cursors={"due": cursor} if cursor else None,
            timezone_name=timezone_name,
        )
        section = (payload.get("sections") or {}).get("due") or {}
        if section.get("status") != "ok":
            raise CollaborationError("Due reminders are temporarily unavailable")
        due = section.get("data") or {}
        next24 = due.get("next24h") or {}
        next3d = due.get("next3d") or {}
        for task in next24.get("items") or []:
            key = (str(task.get("tableId") or ""), str(task.get("recordId") or ""))
            if not all(key):
                continue
            seen_24h.add(key)
            event_key = (key[0], key[1], "due_24h")
            if event_key not in seen_events:
                seen_events.add(event_key)
                output.append({**dict(task), "notificationType": "due_24h"})
        for task in next3d.get("items") or []:
            key = (str(task.get("tableId") or ""), str(task.get("recordId") or ""))
            if not all(key) or key in seen_24h:
                continue
            event_key = (key[0], key[1], "due_3d")
            if event_key not in seen_events:
                seen_events.add(event_key)
                output.append({**dict(task), "notificationType": "due_3d"})
        page24 = next24.get("pageInfo") or {}
        page3d = next3d.get("pageInfo") or {}
        if page3d.get("hasMore"):
            cursor = page3d.get("nextCursor")
        elif page24.get("hasMore"):
            cursor = page24.get("nextCursor")
        else:
            break
        if not cursor:
            break
    return output


async def sync_due_notifications_for_user(
    db: AsyncSession,
    *,
    user_id: int,
    timezone_name: str = "UTC",
) -> int:
    try:
        candidates = await _due_candidate_pages(
            db,
            user_id=user_id,
            timezone_name=timezone_name,
        )
    except MyWorkValidationError as exc:
        raise CollaborationError(str(exc)) from exc
    created_rows: list[UserNotification] = []
    for item in candidates:
        table_id = str(item.get("tableId") or "")
        record_id = str(item.get("recordId") or "")
        notification_type = str(item.get("notificationType") or "")
        due_at = str(item.get("dueAt") or "")
        if not table_id or not record_id or not due_at:
            continue
        event_id = f"{notification_type}:{table_id}:{record_id}:{due_at}"
        notification, is_new = await stage_notification(
            db,
            recipient_user_id=user_id,
            type=notification_type,
            actor_id=None,
            workspace_id=str(item.get("workspaceId")) if item.get("workspaceId") else None,
            table_id=table_id,
            record_id=record_id,
            comment_id=None,
            event_id=event_id,
            dedupe_key=event_id,
            payload={"dueAt": due_at, "timezone": timezone_name},
        )
        if is_new:
            created_rows.append(notification)
    if created_rows:
        await db.commit()
        await publish_notification_rows(created_rows)
    return len(created_rows)


async def sync_user_notifications(
    db: AsyncSession,
    *,
    user_id: int,
    timezone_name: str = "UTC",
) -> Dict[str, int]:
    assignments = await sync_assignment_notifications_for_user(db, user_id=user_id)
    due = await sync_due_notifications_for_user(
        db,
        user_id=user_id,
        timezone_name=timezone_name,
    )
    return {"assignments": assignments, "due": due}
