from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import AutomationEvent, AutomationExecution, AutomationRule
from app.models.change_history import ChangeItem, ChangeSet
from app.models.smart_table import WorkspaceItem


RECORD_EVENT_TYPES = {"record.created", "record.updated"}
EVENT_STATUSES = {"queued", "processing", "processed", "failed"}
MAX_AUTOMATION_DEPTH = 8


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


async def _workspace_id(db: AsyncSession, table_id: str) -> Optional[str]:
    result = await db.execute(
        select(WorkspaceItem.workspace_id).where(WorkspaceItem.id == str(table_id)).limit(1)
    )
    return result.scalars().first()


async def _parent_context(
    db: AsyncSession,
    *,
    source: Optional[str],
    trace_id: Optional[str],
) -> tuple[Optional[str], Optional[str], int]:
    if str(source or "") != "automation" or not trace_id:
        return None, None, 0
    execution = (
        await db.execute(
            select(AutomationExecution)
            .where(AutomationExecution.trace_id == str(trace_id))
            .limit(1)
        )
    ).scalars().first()
    if execution is None:
        return None, None, 0
    return execution.root_event_id, execution.id, int(execution.depth or 0) + 1


async def queue_record_automation_event(
    db: AsyncSession,
    *,
    table_id: str,
    event_type: str,
    record_id: str,
    before_data: Optional[Mapping[str, Any]],
    after_data: Optional[Mapping[str, Any]],
    changed_fields: Iterable[str],
    actor_user_id: Optional[int] = None,
    source: Optional[str] = None,
    trace_id: Optional[str] = None,
    source_change_item_id: Optional[str] = None,
    event_created_at: Optional[datetime] = None,
) -> Optional[AutomationEvent]:
    """Stage one durable record event without running automation inline."""
    normalized_type = str(event_type or "").strip()
    if normalized_type not in RECORD_EVENT_TYPES:
        raise ValueError(f"Unsupported record automation event: {normalized_type}")
    workspace_id = await _workspace_id(db, str(table_id))
    if not workspace_id:
        return None

    event_id = (
        f"evt_change_{source_change_item_id}"
        if source_change_item_id
        else new_event_id()
    )
    existing = None
    if source_change_item_id:
        existing = (
            await db.execute(
                select(AutomationEvent).where(
                    AutomationEvent.source_change_item_id == str(source_change_item_id)
                ).limit(1)
            )
        ).scalars().first()
    if existing is not None:
        return existing

    root_event_id, parent_execution_id, depth = await _parent_context(
        db,
        source=source,
        trace_id=trace_id,
    )
    event = AutomationEvent(
        id=event_id[:128],
        workspace_id=str(workspace_id),
        table_id=str(table_id),
        record_id=str(record_id),
        source_change_item_id=(
            str(source_change_item_id)[:64] if source_change_item_id else None
        ),
        target_automation_id=None,
        type=normalized_type,
        before_data=copy.deepcopy(dict(before_data)) if before_data is not None else None,
        after_data=copy.deepcopy(dict(after_data)) if after_data is not None else None,
        changed_fields=sorted({str(field_id) for field_id in changed_fields}),
        actor_user_id=actor_user_id,
        source=(str(source)[:64] if source else None),
        trace_id=(str(trace_id)[:128] if trace_id else None),
        root_event_id=str(root_event_id or event_id)[:128],
        parent_execution_id=parent_execution_id,
        depth=max(0, int(depth)),
        status="queued",
        available_at=utcnow(),
        created_at=event_created_at or utcnow(),
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
        return event
    except IntegrityError:
        if not source_change_item_id:
            raise
        existing = (
            await db.execute(
                select(AutomationEvent).where(
                    AutomationEvent.source_change_item_id == str(source_change_item_id)
                ).limit(1)
            )
        ).scalars().first()
        if existing is None:
            raise
        return existing


async def materialize_record_events_from_changesets(
    db: AsyncSession,
    *,
    limit: int = 200,
) -> int:
    """Convert committed record ChangeItems into durable automation events.

    The record write and ChangeSet are already one transaction in QTable. The
    worker therefore only needs to persist an idempotent event for ChangeItems
    that have no `source_change_item_id` yet. This keeps automation decoupled
    from every individual record-write API while remaining restart-safe.
    """
    safe_limit = max(1, min(1000, int(limit)))
    result = await db.execute(
        select(ChangeItem, ChangeSet)
        .join(ChangeSet, ChangeSet.id == ChangeItem.change_set_id)
        .outerjoin(
            AutomationEvent,
            AutomationEvent.source_change_item_id == ChangeItem.id,
        )
        .where(
            AutomationEvent.id.is_(None),
            ChangeItem.entity_type == "record",
            ChangeItem.after_data.is_not(None),
            ChangeSet.status == "applied",
        )
        .order_by(ChangeSet.created_at.asc(), ChangeItem.order_index.asc(), ChangeItem.id.asc())
        .limit(safe_limit)
    )
    rows = list(result.all())
    created = 0
    for item, change_set in rows:
        before = dict(item.before_data) if isinstance(item.before_data, Mapping) else None
        after = dict(item.after_data) if isinstance(item.after_data, Mapping) else None
        if after is None:
            continue
        event_type = "record.created" if before is None else "record.updated"
        event = await queue_record_automation_event(
            db,
            table_id=item.table_id,
            event_type=event_type,
            record_id=item.entity_id,
            before_data=before,
            after_data=after,
            changed_fields=list(item.changed_fields or []),
            actor_user_id=change_set.actor_id,
            source=change_set.source,
            trace_id=change_set.trace_id,
            source_change_item_id=item.id,
            event_created_at=change_set.created_at or utcnow(),
        )
        if event is not None:
            created += 1
    if created:
        await db.commit()
    return created


async def queue_synthetic_automation_event(
    db: AsyncSession,
    *,
    event_id: str,
    workspace_id: str,
    table_id: str,
    target_automation_id: str,
    event_type: str,
    record_id: Optional[str] = None,
    after_data: Optional[Mapping[str, Any]] = None,
    actor_user_id: Optional[int] = None,
    source: str = "automation_scheduler",
) -> AutomationEvent:
    """Idempotently stage a deterministic scheduled/due/manual event."""
    existing = (
        await db.execute(
            select(AutomationEvent).where(AutomationEvent.id == str(event_id)).limit(1)
        )
    ).scalars().first()
    if existing is not None:
        return existing
    event = AutomationEvent(
        id=str(event_id)[:128],
        workspace_id=str(workspace_id),
        table_id=str(table_id),
        record_id=str(record_id) if record_id is not None else None,
        source_change_item_id=None,
        target_automation_id=str(target_automation_id)[:64],
        type=str(event_type)[:32],
        before_data=None,
        after_data=copy.deepcopy(dict(after_data)) if after_data is not None else None,
        changed_fields=[],
        actor_user_id=actor_user_id,
        source=str(source)[:64],
        root_event_id=str(event_id)[:128],
        depth=0,
        status="queued",
        available_at=utcnow(),
        created_at=utcnow(),
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
        return event
    except IntegrityError:
        existing = (
            await db.execute(
                select(AutomationEvent).where(AutomationEvent.id == str(event_id)).limit(1)
            )
        ).scalars().first()
        if existing is None:
            raise
        return existing


def trigger_matches(rule: AutomationRule, event: AutomationEvent) -> bool:
    if event.target_automation_id and str(event.target_automation_id) != str(rule.id):
        return False
    trigger = dict(rule.trigger or {})
    trigger_type = str(trigger.get("type") or "")
    if trigger_type != str(event.type):
        return False
    if trigger_type != "record.updated":
        return True

    configured_fields = {str(value) for value in trigger.get("fieldIds") or []}
    changed = {str(value) for value in event.changed_fields or []}
    if configured_fields and not configured_fields.intersection(changed):
        return False

    if "from" in trigger or "to" in trigger:
        if len(configured_fields) != 1:
            return False
        field_id = next(iter(configured_fields))
        before = dict(event.before_data or {}).get(field_id)
        after = dict(event.after_data or {}).get(field_id)
        if "from" in trigger and before != trigger.get("from"):
            return False
        if "to" in trigger and after != trigger.get("to"):
            return False
    return True


def serialize_event(event: AutomationEvent) -> Dict[str, Any]:
    return {
        "id": event.id,
        "workspaceId": event.workspace_id,
        "tableId": event.table_id,
        "recordId": event.record_id,
        "sourceChangeItemId": event.source_change_item_id,
        "targetAutomationId": event.target_automation_id,
        "type": event.type,
        "changedFields": list(event.changed_fields or []),
        "source": event.source,
        "traceId": event.trace_id,
        "rootEventId": event.root_event_id,
        "parentExecutionId": event.parent_execution_id,
        "depth": int(event.depth or 0),
        "status": event.status,
        "availableAt": event.available_at.isoformat() if event.available_at else None,
        "createdAt": event.created_at.isoformat() if event.created_at else None,
        "processedAt": event.processed_at.isoformat() if event.processed_at else None,
        "errorCode": event.error_code,
        "errorMessage": event.error_message,
    }
