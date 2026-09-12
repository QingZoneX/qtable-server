from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_history import ChangeItem, ChangeSet, RecycleBinRecord
from app.models.smart_table import TableRecord, WorkspaceItem


class ChangeConflictError(ValueError):
    pass


class ChangeNotUndoableError(ValueError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_version(record: TableRecord) -> int:
    try:
        return max(1, int(record.version or 1))
    except (TypeError, ValueError):
        return 1


def record_meta(record: TableRecord) -> Dict[str, Any]:
    return {
        "orderIndex": int(record.order_index or 0),
        "createdByUserId": record.created_by_user_id,
    }


def changed_fields(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> List[str]:
    if before is None or after is None:
        source = after if before is None else before
        return sorted(str(key) for key in (source or {}).keys())
    keys = set(before.keys()) | set(after.keys())
    return sorted(str(key) for key in keys if before.get(key) != after.get(key))


async def _workspace_id_for_table(db: AsyncSession, table_id: Optional[str]) -> Optional[str]:
    if not table_id:
        return None
    result = await db.execute(
        select(WorkspaceItem.workspace_id)
        .where(WorkspaceItem.id == table_id)
        .limit(1)
    )
    return result.scalars().first()


async def append_change_set(
    db: AsyncSession,
    *,
    table_id: Optional[str],
    actor_id: Optional[int],
    operation: str,
    items: Sequence[Dict[str, Any]],
    actor_type: str = "user",
    source: Optional[str] = None,
    trace_id: Optional[str] = None,
    summary: str = "",
    status: str = "applied",
    parent_change_set_id: Optional[str] = None,
    change_set_id: Optional[str] = None,
) -> ChangeSet:
    change_id = change_set_id or _id("chg")
    change_set = ChangeSet(
        id=change_id,
        workspace_id=await _workspace_id_for_table(db, table_id),
        table_id=table_id,
        actor_type=(actor_type or "user")[:32],
        actor_id=actor_id,
        operation=(operation or "update")[:64],
        source=(source or None),
        trace_id=(trace_id or None),
        summary=summary or "",
        status=status,
        parent_change_set_id=parent_change_set_id,
    )
    db.add(change_set)
    for index, item in enumerate(items):
        db.add(
            ChangeItem(
                id=_id("chi"),
                change_set_id=change_id,
                table_id=str(item.get("table_id") or table_id or ""),
                entity_type=str(item.get("entity_type") or "record"),
                entity_id=str(item.get("entity_id") or ""),
                before_data=copy.deepcopy(item.get("before_data")),
                after_data=copy.deepcopy(item.get("after_data")),
                before_meta=copy.deepcopy(item.get("before_meta")),
                after_meta=copy.deepcopy(item.get("after_meta")),
                version_before=item.get("version_before"),
                version_after=item.get("version_after"),
                changed_fields=copy.deepcopy(
                    item.get("changed_fields")
                    if item.get("changed_fields") is not None
                    else changed_fields(item.get("before_data"), item.get("after_data"))
                ),
                order_index=index,
            )
        )
    await db.flush()
    return change_set


def serialize_change_set(change_set: ChangeSet) -> Dict[str, Any]:
    return {
        "id": change_set.id,
        "workspaceId": change_set.workspace_id,
        "tableId": change_set.table_id,
        "actorType": change_set.actor_type,
        "actorId": change_set.actor_id,
        "operation": change_set.operation,
        "source": change_set.source,
        "traceId": change_set.trace_id,
        "summary": change_set.summary,
        "status": change_set.status,
        "parentChangeSetId": change_set.parent_change_set_id,
        "createdAt": change_set.created_at.isoformat() if change_set.created_at else None,
        "undoneAt": change_set.undone_at.isoformat() if change_set.undone_at else None,
        "undoneByUserId": change_set.undone_by_user_id,
    }


async def get_change_set(db: AsyncSession, change_set_id: str) -> Optional[ChangeSet]:
    result = await db.execute(
        select(ChangeSet).where(ChangeSet.id == change_set_id).limit(1)
    )
    return result.scalars().first()


async def get_change_items(db: AsyncSession, change_set_id: str) -> List[ChangeItem]:
    result = await db.execute(
        select(ChangeItem)
        .where(ChangeItem.change_set_id == change_set_id)
        .order_by(ChangeItem.order_index.asc(), ChangeItem.id.asc())
    )
    return list(result.scalars().all())


async def list_record_history(
    db: AsyncSession,
    *,
    table_id: str,
    record_id: str,
    offset: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(200, int(limit)))
    base_conditions = (
        ChangeItem.table_id == table_id,
        ChangeItem.entity_type == "record",
        ChangeItem.entity_id == str(record_id),
    )
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ChangeItem)
                .where(*base_conditions)
            )
        ).scalar()
        or 0
    )
    result = await db.execute(
        select(ChangeItem, ChangeSet)
        .join(ChangeSet, ChangeSet.id == ChangeItem.change_set_id)
        .where(*base_conditions)
        .order_by(ChangeSet.created_at.desc(), ChangeItem.order_index.desc())
        .offset(safe_offset)
        .limit(safe_limit)
    )
    items: List[Dict[str, Any]] = []
    for item, change_set in result.all():
        items.append(
            {
                "changeSet": serialize_change_set(change_set),
                "entityType": item.entity_type,
                "entityId": item.entity_id,
                "before": copy.deepcopy(item.before_data),
                "after": copy.deepcopy(item.after_data),
                "beforeMeta": copy.deepcopy(item.before_meta),
                "afterMeta": copy.deepcopy(item.after_meta),
                "versionBefore": item.version_before,
                "versionAfter": item.version_after,
                "changedFields": list(item.changed_fields or []),
            }
        )
    return {
        "items": items,
        "totalCount": total,
        "offset": safe_offset,
        "limit": safe_limit,
        "hasMore": safe_offset + len(items) < total,
    }


async def list_recycle_bin(
    db: AsyncSession,
    *,
    table_id: str,
    offset: int = 0,
    limit: int = 100,
) -> Dict[str, Any]:
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(200, int(limit)))
    conditions = (
        RecycleBinRecord.table_id == table_id,
        RecycleBinRecord.status == "deleted",
    )
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(RecycleBinRecord)
                .where(*conditions)
            )
        ).scalar()
        or 0
    )
    result = await db.execute(
        select(RecycleBinRecord)
        .where(*conditions)
        .order_by(RecycleBinRecord.deleted_at.desc(), RecycleBinRecord.id.desc())
        .offset(safe_offset)
        .limit(safe_limit)
    )
    items = [
        {
            "recycleId": entry.id,
            "tableId": entry.table_id,
            "recordId": entry.record_id,
            "data": copy.deepcopy(entry.data),
            "orderIndex": entry.order_index,
            "createdByUserId": entry.created_by_user_id,
            "recordVersion": entry.record_version,
            "deletedByUserId": entry.deleted_by_user_id,
            "deletedAt": entry.deleted_at.isoformat() if entry.deleted_at else None,
            "changeSetId": entry.change_set_id,
        }
        for entry in result.scalars().all()
    ]
    return {
        "items": items,
        "totalCount": total,
        "offset": safe_offset,
        "limit": safe_limit,
        "hasMore": safe_offset + len(items) < total,
    }


async def _active_record(
    db: AsyncSession,
    table_id: str,
    record_id: str,
    *,
    lock: bool = False,
) -> Optional[TableRecord]:
    statement = select(TableRecord).where(
        TableRecord.table_id == table_id,
        TableRecord.id == str(record_id),
    )
    if lock:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return result.scalars().first()


async def _deleted_entry_for_change(
    db: AsyncSession,
    table_id: str,
    record_id: str,
    change_set_id: str,
    *,
    lock: bool = False,
) -> Optional[RecycleBinRecord]:
    statement = select(RecycleBinRecord).where(
        RecycleBinRecord.table_id == table_id,
        RecycleBinRecord.record_id == str(record_id),
        RecycleBinRecord.change_set_id == change_set_id,
        RecycleBinRecord.status == "deleted",
    )
    if lock:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return result.scalars().first()


async def undo_change_set(
    db: AsyncSession,
    *,
    change_set_id: str,
    actor_id: Optional[int],
    actor_type: str = "user",
    source: str = "undo",
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    change_result = await db.execute(
        select(ChangeSet)
        .where(ChangeSet.id == change_set_id)
        .with_for_update()
    )
    original = change_result.scalars().first()
    if not original:
        raise ChangeNotUndoableError("ChangeSet not found")
    if original.status != "applied":
        raise ChangeNotUndoableError("ChangeSet is not in an applied state")
    if original.operation == "purge":
        raise ChangeNotUndoableError("Permanent purge cannot be undone")

    items = await get_change_items(db, original.id)
    if not items:
        raise ChangeNotUndoableError("ChangeSet has no reversible items")

    active_by_key: Dict[tuple[str, str], TableRecord] = {}
    recycle_by_key: Dict[tuple[str, str], RecycleBinRecord] = {}

    # Validate every entity before changing any of them. This makes undo atomic
    # and prevents an old history entry from overwriting newer work.
    for item in items:
        if item.entity_type != "record":
            raise ChangeNotUndoableError(
                f"Unsupported undo entity type: {item.entity_type}"
            )
        key = (item.table_id, item.entity_id)
        active = await _active_record(db, *key, lock=True)
        was_delete = item.before_data is not None and item.after_data is None
        if was_delete:
            if active is not None:
                raise ChangeConflictError(
                    f"Record {item.entity_id} was recreated after deletion"
                )
            recycle = await _deleted_entry_for_change(
                db,
                item.table_id,
                item.entity_id,
                original.id,
                lock=True,
            )
            if recycle is None:
                raise ChangeConflictError(
                    f"Deleted record {item.entity_id} is no longer restorable"
                )
            expected = int(item.version_after or recycle.record_version or 1)
            if int(recycle.record_version or 1) != expected:
                raise ChangeConflictError(
                    f"Record {item.entity_id} version changed after deletion"
                )
            recycle_by_key[key] = recycle
            continue

        if active is None:
            raise ChangeConflictError(
                f"Record {item.entity_id} no longer exists"
            )
        expected_version = int(item.version_after or 1)
        if record_version(active) != expected_version:
            raise ChangeConflictError(
                f"Record {item.entity_id} has newer changes"
            )
        active_by_key[key] = active

    undo_id = _id("chg")
    inverse_items: List[Dict[str, Any]] = []

    # Restore deleted records first so relation-cleanup items can safely point
    # back at the target record again.
    for item in items:
        if not (item.before_data is not None and item.after_data is None):
            continue
        key = (item.table_id, item.entity_id)
        recycle = recycle_by_key[key]
        previous_version = int(item.version_after or recycle.record_version or 1)
        next_version = previous_version + 1
        before_meta = dict(item.before_meta or {})
        restored = TableRecord(
            id=item.entity_id,
            table_id=item.table_id,
            data=copy.deepcopy(item.before_data or {}),
            order_index=int(before_meta.get("orderIndex") or recycle.order_index or 0),
            created_by_user_id=before_meta.get(
                "createdByUserId", recycle.created_by_user_id
            ),
            version=next_version,
        )
        db.add(restored)
        recycle.status = "restored"
        recycle.restored_at = _utcnow()
        recycle.restored_by_user_id = actor_id
        inverse_items.append(
            {
                "table_id": item.table_id,
                "entity_type": "record",
                "entity_id": item.entity_id,
                "before_data": None,
                "after_data": copy.deepcopy(restored.data),
                "before_meta": None,
                "after_meta": record_meta(restored),
                "version_before": previous_version,
                "version_after": next_version,
                "changed_fields": changed_fields(None, restored.data),
            }
        )

    # Reverse normal record updates.
    for item in items:
        if item.before_data is None or item.after_data is None:
            continue
        key = (item.table_id, item.entity_id)
        record = active_by_key[key]
        current_data = copy.deepcopy(record.data or {})
        current_meta = record_meta(record)
        current_version = record_version(record)
        record.data = copy.deepcopy(item.before_data or {})
        before_meta = dict(item.before_meta or {})
        if "orderIndex" in before_meta:
            record.order_index = int(before_meta["orderIndex"] or 0)
        if "createdByUserId" in before_meta:
            record.created_by_user_id = before_meta.get("createdByUserId")
        record.version = current_version + 1
        inverse_items.append(
            {
                "table_id": item.table_id,
                "entity_type": "record",
                "entity_id": item.entity_id,
                "before_data": current_data,
                "after_data": copy.deepcopy(record.data),
                "before_meta": current_meta,
                "after_meta": record_meta(record),
                "version_before": current_version,
                "version_after": record.version,
                "changed_fields": changed_fields(current_data, record.data),
            }
        )

    # Undo creates last: the inverse is a recoverable delete, not a hard purge.
    for item in items:
        if not (item.before_data is None and item.after_data is not None):
            continue
        key = (item.table_id, item.entity_id)
        record = active_by_key[key]
        current_data = copy.deepcopy(record.data or {})
        current_meta = record_meta(record)
        current_version = record_version(record)
        deleted_version = current_version + 1
        db.add(
            RecycleBinRecord(
                id=_id("rcy"),
                table_id=item.table_id,
                record_id=item.entity_id,
                data=current_data,
                order_index=int(record.order_index or 0),
                created_by_user_id=record.created_by_user_id,
                record_version=deleted_version,
                deleted_by_user_id=actor_id,
                change_set_id=undo_id,
                status="deleted",
            )
        )
        await db.delete(record)
        inverse_items.append(
            {
                "table_id": item.table_id,
                "entity_type": "record",
                "entity_id": item.entity_id,
                "before_data": current_data,
                "after_data": None,
                "before_meta": current_meta,
                "after_meta": None,
                "version_before": current_version,
                "version_after": deleted_version,
                "changed_fields": changed_fields(current_data, None),
            }
        )

    undo_set = await append_change_set(
        db,
        table_id=original.table_id,
        actor_id=actor_id,
        actor_type=actor_type,
        operation="undo",
        source=source,
        trace_id=trace_id,
        summary=f"Undo {original.operation}: {original.summary or original.id}",
        parent_change_set_id=original.id,
        change_set_id=undo_id,
        items=inverse_items,
    )
    original.status = "undone"
    original.undone_at = _utcnow()
    original.undone_by_user_id = actor_id
    await db.commit()
    await db.refresh(undo_set)
    return {
        "changeSet": serialize_change_set(undo_set),
        "undoneChangeSetId": original.id,
        "affectedCount": len(inverse_items),
        "affectedTableIds": sorted({item.table_id for item in items}),
    }


async def restore_recycled_record(
    db: AsyncSession,
    *,
    table_id: str,
    record_id: str,
    actor_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    result = await db.execute(
        select(RecycleBinRecord)
        .where(
            RecycleBinRecord.table_id == table_id,
            RecycleBinRecord.record_id == str(record_id),
            RecycleBinRecord.status == "deleted",
        )
        .order_by(RecycleBinRecord.deleted_at.desc(), RecycleBinRecord.id.desc())
        .limit(1)
    )
    entry = result.scalars().first()
    if not entry:
        return None
    if not entry.change_set_id:
        raise ChangeNotUndoableError("Recycle entry has no reversible ChangeSet")
    return await undo_change_set(
        db,
        change_set_id=entry.change_set_id,
        actor_id=actor_id,
        source="recycle_restore",
    )


async def purge_recycled_record(
    db: AsyncSession,
    *,
    table_id: str,
    record_id: str,
    actor_id: Optional[int],
) -> bool:
    result = await db.execute(
        select(RecycleBinRecord)
        .where(
            RecycleBinRecord.table_id == table_id,
            RecycleBinRecord.record_id == str(record_id),
            RecycleBinRecord.status == "deleted",
        )
        .order_by(RecycleBinRecord.deleted_at.desc(), RecycleBinRecord.id.desc())
        .limit(1)
        .with_for_update()
    )
    entry = result.scalars().first()
    if not entry:
        return False

    # Permanent deletion must also remove snapshots from history; otherwise a
    # "purged" record would still be recoverable from ChangeItem JSON.
    if entry.change_set_id:
        origin_result = await db.execute(
            select(ChangeSet)
            .where(ChangeSet.id == entry.change_set_id)
            .with_for_update()
        )
        origin_change_set = origin_result.scalars().first()
        if origin_change_set is not None:
            origin_change_set.status = "irreversible"

    history_result = await db.execute(
        select(ChangeItem).where(
            ChangeItem.table_id == table_id,
            ChangeItem.entity_type == "record",
            ChangeItem.entity_id == str(record_id),
        )
    )
    for item in history_result.scalars().all():
        item.before_data = None
        item.after_data = None
        item.before_meta = None
        item.after_meta = None
        item.changed_fields = ["__purged__"]

    await append_change_set(
        db,
        table_id=table_id,
        actor_id=actor_id,
        actor_type="user",
        operation="purge",
        source="recycle_bin",
        summary=f"Permanently purge record {record_id}",
        status="irreversible",
        items=[
            {
                "table_id": table_id,
                "entity_type": "record",
                "entity_id": str(record_id),
                "before_data": None,
                "after_data": None,
                "before_meta": None,
                "after_meta": {"purged": True},
                "version_before": entry.record_version,
                "version_after": entry.record_version,
                "changed_fields": ["__purged__"],
            }
        ],
    )
    # Remove every recycle snapshot for this logical record, including
    # previously restored entries from older delete/restore cycles.
    all_recycle_result = await db.execute(
        select(RecycleBinRecord).where(
            RecycleBinRecord.table_id == table_id,
            RecycleBinRecord.record_id == str(record_id),
        )
    )
    for recycle_entry in all_recycle_result.scalars().all():
        await db.delete(recycle_entry)
    await db.commit()
    return True
