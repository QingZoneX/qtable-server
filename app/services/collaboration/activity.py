from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_history import ChangeItem, ChangeSet
from app.models.task_profile import TableTaskProfile
from app.services.collaboration.common import (
    actor_map,
    actor_payload,
    decode_offset_cursor,
    encode_offset_cursor,
    record_deep_link,
    safe_limit,
    table_item,
)

ACTIVITY_SCAN_CAP = 3000


def _comment_record_id(item: ChangeItem) -> Optional[str]:
    for value in (item.after_data, item.before_data):
        if isinstance(value, Mapping) and value.get("recordId"):
            return str(value.get("recordId"))
    return None


def _comment_parent_id(item: ChangeItem) -> Optional[str]:
    for value in (item.after_data, item.before_data):
        if isinstance(value, Mapping) and value.get("parentCommentId"):
            return str(value.get("parentCommentId"))
    return None


def _semantic_fields(profile: Optional[TableTaskProfile]) -> Dict[str, str]:
    if profile is None or not isinstance(profile.config, Mapping):
        return {}
    mapping = {
        "statusFieldId": "status",
        "assigneeFieldId": "assignee",
        "dueDateFieldId": "due",
        "priorityFieldId": "priority",
        "progressFieldId": "progress",
    }
    return {
        str(profile.config[key]): kind
        for key, kind in mapping.items()
        if profile.config.get(key)
    }


def _record_activity_summary(
    item: ChangeItem,
    change_set: ChangeSet,
    semantic: Mapping[str, str],
) -> tuple[list[str], str]:
    if item.before_data is None and item.after_data is not None:
        return ["record.created"], "创建了记录"
    if item.before_data is not None and item.after_data is None:
        return ["record.deleted"], "删除了记录"

    changed = {str(value) for value in (item.changed_fields or [])}
    kinds: list[str] = []
    labels: list[str] = []
    label_by_kind = {
        "status": "状态",
        "assignee": "负责人",
        "due": "截止日期",
        "priority": "优先级",
        "progress": "进度",
    }
    event_by_kind = {
        "status": "task.status_changed",
        "assignee": "task.assignee_changed",
        "due": "task.due_changed",
        "priority": "task.priority_changed",
        "progress": "task.progress_changed",
    }
    for field_id, semantic_kind in semantic.items():
        if field_id in changed:
            kinds.append(event_by_kind[semantic_kind])
            labels.append(label_by_kind[semantic_kind])
    if not kinds:
        return ["record.updated"], "更新了记录"
    return kinds, f"更新了{'、'.join(labels)}"


def _activity_payload(
    *,
    item: ChangeItem,
    change_set: ChangeSet,
    actor: Dict[str, Any],
    kinds: list[str],
    summary: str,
    deep_link: str,
) -> Dict[str, Any]:
    return {
        "id": f"{change_set.id}:{item.id}",
        "changeSetId": change_set.id,
        "commentId": item.entity_id if item.entity_type == "comment" else None,
        "actor": actor,
        "kinds": kinds,
        "operation": change_set.operation,
        "source": change_set.source,
        "summary": summary,
        "deepLink": deep_link,
        "createdAt": change_set.created_at.isoformat() if change_set.created_at else None,
    }


async def list_record_activity(
    db: AsyncSession,
    *,
    table_id: str,
    record_id: str,
    cursor: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Merge record and comment ChangeSets without exposing raw field values.

    The resolver checks current record permission before calling this service.
    We still filter every ChangeItem to this exact record, and summaries use
    only Task Profile field IDs rather than raw before/after data.
    """
    safe = safe_limit(limit)
    raw_offset = decode_offset_cursor(cursor)
    table = await table_item(db, table_id)
    profile = (
        await db.execute(
            select(TableTaskProfile).where(TableTaskProfile.table_id == table_id).limit(1)
        )
    ).scalars().first()
    semantic = _semantic_fields(profile)

    visible: list[tuple[ChangeItem, ChangeSet, list[str], str]] = []
    scanned = 0
    has_more = False
    batch_size = min(300, max(100, safe * 8))

    while len(visible) < safe and scanned < ACTIVITY_SCAN_CAP:
        result = await db.execute(
            select(ChangeItem, ChangeSet)
            .join(ChangeSet, ChangeSet.id == ChangeItem.change_set_id)
            .where(
                ChangeItem.table_id == table_id,
                ChangeItem.entity_type.in_(["record", "comment"]),
                ChangeSet.status == "applied",
            )
            .order_by(
                ChangeSet.created_at.desc(),
                ChangeItem.order_index.desc(),
                ChangeItem.id.desc(),
            )
            .offset(raw_offset)
            .limit(batch_size)
        )
        rows = list(result.all())
        if not rows:
            has_more = False
            break

        processed = 0
        for item, change_set in rows:
            processed += 1
            raw_offset += 1
            scanned += 1
            if item.entity_type == "record":
                if str(item.entity_id) != str(record_id):
                    continue
                kinds, summary = _record_activity_summary(
                    item,
                    change_set,
                    semantic,
                )
            else:
                if _comment_record_id(item) != str(record_id):
                    continue
                operation = str(change_set.operation or "")
                parent = _comment_parent_id(item)
                if operation == "comment.create":
                    kinds = ["comment.reply" if parent else "comment.created"]
                    summary = "回复了评论" if parent else "发表了评论"
                elif operation == "comment.update":
                    kinds = ["comment.updated"]
                    summary = "编辑了评论"
                elif operation == "comment.delete":
                    kinds = ["comment.deleted"]
                    summary = "删除了评论"
                else:
                    kinds = ["comment.changed"]
                    summary = "更新了评论"
            visible.append((item, change_set, kinds, summary))
            if len(visible) >= safe or scanned >= ACTIVITY_SCAN_CAP:
                break

        if processed < len(rows):
            has_more = True
            break
        if len(rows) < batch_size:
            has_more = False
            break
        has_more = True

    actor_ids = [change_set.actor_id for _, change_set, _, _ in visible]
    actors = await actor_map(db, actor_ids)
    deep_link = await record_deep_link(
        db,
        table=table,
        record_id=record_id,
    )
    items: list[Dict[str, Any]] = []
    for item, change_set, kinds, summary in visible[:safe]:
        actor = (
            actors.get(int(change_set.actor_id))
            if change_set.actor_id is not None
            else None
        )
        item_link = deep_link
        if item.entity_type == "comment":
            item_link = await record_deep_link(
                db,
                table=table,
                record_id=record_id,
                comment_id=str(item.entity_id),
            )
        items.append(
            _activity_payload(
                item=item,
                change_set=change_set,
                actor=actor_payload(actor, change_set.actor_id),
                kinds=kinds,
                summary=summary,
                deep_link=item_link,
            )
        )
    return {
        "items": items,
        "pageInfo": {
            "hasMore": bool(has_more),
            "nextCursor": encode_offset_cursor(raw_offset) if has_more else None,
            "scannedCount": scanned,
        },
    }
