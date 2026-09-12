"""Permission-safe Python fallback for My Work KPI aggregation.

The primary My Work KPI path is SQL-first for performance. This module is used only
when that section fails on legacy/malformed field values that database casts cannot
consume. It deliberately reuses current Task Profile mappings and row-permission
semantics instead of guessing fields.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any, Dict, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_history import ChangeItem, ChangeSet
from app.models.smart_table import TableRecord
from app.services.my_work import (
    TaskTableContext,
    _parse_due,
    _task_table_contexts,
    _timezone,
    _utcnow,
)
from app.services.row_permissions import record_is_visible


def _field_value(data: Mapping[str, Any], field_id: Any) -> Any:
    return data.get(str(field_id)) if field_id else None


def _member_contains_user(value: Any, user_id: int) -> bool:
    target = str(user_id)
    if isinstance(value, list):
        return any(str(part) == target for part in value if part is not None)
    return value is not None and str(value) == target


def _status_values(ctx: TaskTableContext, key: str) -> set[str]:
    return {str(value) for value in ctx.config.get(key) or []}


def _is_completed(ctx: TaskTableContext, data: Mapping[str, Any]) -> bool:
    status_field = ctx.config.get("statusFieldId")
    completed = _status_values(ctx, "completedStatusValues")
    if not status_field or not completed:
        return False
    return str(_field_value(data, status_field)) in completed


def _is_blocked(ctx: TaskTableContext, data: Mapping[str, Any]) -> bool:
    status_field = ctx.config.get("statusFieldId")
    blocked = _status_values(ctx, "blockedStatusValues")
    if not status_field or not blocked:
        return False
    return str(_field_value(data, status_field)) in blocked


def _is_overdue(ctx: TaskTableContext, data: Mapping[str, Any], *, tz) -> bool:
    due_field = ctx.config.get("dueDateFieldId")
    if not due_field or _is_completed(ctx, data):
        return False
    due = _parse_due(_field_value(data, due_field), tz)
    return due is not None and due < _utcnow().astimezone(tz)


def _visible(ctx: TaskTableContext, record: TableRecord, user_id: int) -> bool:
    return record_is_visible(
        policy=ctx.row_policy,
        user_id=user_id,
        table_permission=ctx.permission,
        created_by_user_id=record.created_by_user_id,
        data=dict(record.data or {}),
    )


async def _visible_records(
    db: AsyncSession,
    *,
    ctx: TaskTableContext,
    user_id: int,
) -> list[TableRecord]:
    result = await db.execute(
        select(TableRecord).where(TableRecord.table_id == ctx.table_id)
    )
    return [
        record
        for record in result.scalars().all()
        if _visible(ctx, record, user_id)
    ]


def _project_progress(
    ctx: TaskTableContext,
    records: Sequence[TableRecord],
    *,
    completed_count: int,
) -> float:
    progress_field = ctx.config.get("progressFieldId")
    numeric_values: list[float] = []
    if progress_field:
        for record in records:
            raw = _field_value(dict(record.data or {}), progress_field)
            if raw in (None, "") or isinstance(raw, bool):
                continue
            try:
                numeric_values.append(float(raw))
            except (TypeError, ValueError):
                # Legacy malformed progress values are ignored rather than
                # turning the whole Home KPI section into a SQL error.
                continue
    if numeric_values:
        return round(sum(numeric_values) / len(numeric_values), 2)
    return round((completed_count / len(records)) * 100, 2) if records else 0.0


async def _completed_this_week(
    db: AsyncSession,
    *,
    contexts: Sequence[TaskTableContext],
    visible_ids_by_table: Mapping[str, set[str]],
    user_id: int,
    tz,
) -> int:
    now_local = _utcnow().astimezone(tz)
    week_start_local = (
        now_local - timedelta(days=now_local.weekday())
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_utc = week_start_local.astimezone(timezone.utc)
    completed_records: set[tuple[str, str]] = set()

    for ctx in contexts:
        status_field = ctx.config.get("statusFieldId")
        assignee_field = ctx.config.get("assigneeFieldId")
        completed = _status_values(ctx, "completedStatusValues")
        if not status_field or not assignee_field or not completed:
            continue
        visible_ids = visible_ids_by_table.get(ctx.table_id, set())
        if not visible_ids:
            continue

        result = await db.execute(
            select(ChangeItem, ChangeSet)
            .join(ChangeSet, ChangeSet.id == ChangeItem.change_set_id)
            .where(
                ChangeItem.table_id == ctx.table_id,
                ChangeItem.entity_type == "record",
                ChangeSet.created_at >= week_start_utc,
                ChangeSet.status == "applied",
            )
        )
        for item, _change_set in result.all():
            record_id = str(item.entity_id)
            if record_id not in visible_ids:
                continue
            changed = {str(value) for value in item.changed_fields or []}
            if str(status_field) not in changed:
                continue
            after = item.after_data if isinstance(item.after_data, Mapping) else {}
            if str(_field_value(after, status_field)) not in completed:
                continue
            if not _member_contains_user(_field_value(after, assignee_field), user_id):
                continue
            completed_records.add((ctx.table_id, record_id))

    return len(completed_records)


async def build_my_work_kpi_fallback(
    db: AsyncSession,
    *,
    user_id: int,
    timezone_name: str,
) -> Dict[str, Any]:
    """Recompute the four KPI values without database JSON numeric/date casts."""
    tz = _timezone(timezone_name)
    contexts = await _task_table_contexts(db, user_id=user_id)
    my_incomplete = 0
    risk = 0
    active_projects = 0
    visible_ids_by_table: dict[str, set[str]] = {}

    for ctx in contexts:
        records = await _visible_records(db, ctx=ctx, user_id=user_id)
        visible_ids_by_table[ctx.table_id] = {str(record.id) for record in records}

        completed_count = 0
        for record in records:
            data = dict(record.data or {})
            completed = _is_completed(ctx, data)
            if completed:
                completed_count += 1
            assignee_field = ctx.config.get("assigneeFieldId")
            assigned = bool(
                assignee_field
                and _member_contains_user(_field_value(data, assignee_field), user_id)
            )
            if assigned and not completed:
                my_incomplete += 1
                if _is_blocked(ctx, data) or _is_overdue(ctx, data, tz=tz):
                    risk += 1

        # Compute progress as part of the fallback path as a deliberate
        # validation of legacy progress values, even though KPI needs only
        # incompleteTasks to decide whether the project is active.
        _project_progress(
            ctx,
            records,
            completed_count=completed_count,
        )
        if len(records) - completed_count > 0:
            active_projects += 1

    completed_this_week = await _completed_this_week(
        db,
        contexts=contexts,
        visible_ids_by_table=visible_ids_by_table,
        user_id=user_id,
        tz=tz,
    )
    return {
        "myIncompleteCount": my_incomplete,
        "completedThisWeekCount": completed_this_week,
        "activeProjectCount": active_projects,
        "overdueOrRiskCount": risk,
    }
