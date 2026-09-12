from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_history import ChangeItem, ChangeSet
from app.models.smart_table import TableRecord, WorkspaceItem
from app.services.member_field import workspace_member_options_for_table
from app.services.task_profile import get_task_profile_record


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "titleFieldId": ("任务名称", "任务", "标题", "名称", "title", "task", "name"),
    "statusFieldId": ("任务状态", "状态", "status", "state", "stage"),
    "assigneeFieldId": ("负责人", "执行人", "成员", "assignee", "owner", "member"),
    "priorityFieldId": ("优先级", "priority"),
    "dueDateFieldId": ("截止日期", "到期时间", "到期日期", "due", "deadline", "end date"),
}


def _normalized_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _field_map(fields: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {
        str(field.get("id")): field
        for field in fields
        if field.get("id") is not None
    }


def _semantic_field(
    fields: Sequence[Mapping[str, Any]],
    profile: Optional[Mapping[str, Any]],
    key: str,
) -> Optional[Mapping[str, Any]]:
    by_id = _field_map(fields)
    if profile is not None:
        field_id = profile.get(key)
        return by_id.get(str(field_id)) if field_id else None

    aliases = _FIELD_ALIASES[key]
    for field in fields:
        name = _normalized_name(field.get("name"))
        if any(alias in name for alias in aliases):
            return field
    return None


def _option_label(field: Mapping[str, Any], value: Any) -> Optional[str]:
    if value in (None, "", []):
        return None
    options = field.get("options")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, Mapping):
                continue
            candidates = {
                str(candidate)
                for candidate in (
                    option.get("id"),
                    option.get("value"),
                    option.get("label"),
                    option.get("name"),
                )
                if candidate is not None
            }
            if str(value) in candidates:
                return str(
                    option.get("label")
                    or option.get("name")
                    or option.get("value")
                    or option.get("id")
                )
    return str(value)


def _scalar_display(field: Optional[Mapping[str, Any]], record: Mapping[str, Any]) -> Optional[str]:
    if field is None or field.get("id") is None:
        return None
    value = record.get(str(field["id"]))
    if value in (None, "", []):
        return None
    if str(field.get("type") or "").lower() in {"select", "single_select"}:
        return _option_label(field, value)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item not in (None, "")) or None
    return str(value)


def _member_ids(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_member_ids(item))
        return list(dict.fromkeys(output))
    if isinstance(value, Mapping):
        candidate = value.get("id") or value.get("userId") or value.get("user_id") or value.get("value")
        return _member_ids(candidate)
    return [str(value)]


async def _assignee_display(
    db: AsyncSession,
    table_id: str,
    field: Optional[Mapping[str, Any]],
    record: Mapping[str, Any],
) -> Optional[str]:
    if field is None or field.get("id") is None:
        return None
    value = record.get(str(field["id"]))
    if value in (None, "", []):
        return None
    if str(field.get("type") or "").lower() != "member":
        return _scalar_display(field, record)

    ids = _member_ids(value)
    if not ids:
        return None
    options = await workspace_member_options_for_table(db, table_id)
    labels = {str(option["id"]): str(option.get("label") or option["id"]) for option in options}
    visible = [labels.get(user_id, user_id) for user_id in ids]
    return ", ".join(visible) if visible else None


async def latest_record_change_at(
    db: AsyncSession,
    *,
    table_id: str,
    record_id: str,
) -> Optional[datetime]:
    result = await db.execute(
        select(ChangeSet.created_at)
        .join(ChangeItem, ChangeItem.change_set_id == ChangeSet.id)
        .where(
            ChangeItem.table_id == table_id,
            ChangeItem.entity_type == "record",
            ChangeItem.entity_id == record_id,
        )
        .order_by(ChangeSet.created_at.desc(), ChangeItem.order_index.desc())
        .limit(1)
    )
    return result.scalar()


async def build_qnote_task_snapshot(
    db: AsyncSession,
    *,
    table: WorkspaceItem,
    record_model: TableRecord,
    fields: Sequence[Mapping[str, Any]],
    qtable_url: str,
) -> dict[str, Any]:
    """Project a permission-checked QTable record into the QNote task contract.

    The caller is responsible for table/row authorization before invoking this
    projector. A configured Task Profile is authoritative: if a semantic field
    is intentionally unmapped or no longer exists, the projector returns null
    instead of guessing a different column. Legacy tables without a profile use
    conservative aliases for backwards compatibility.
    """

    profile_record = await get_task_profile_record(db, table.id)
    profile = dict(profile_record.config or {}) if profile_record is not None else None
    record = dict(record_model.data or {})

    title_field = _semantic_field(fields, profile, "titleFieldId")
    status_field = _semantic_field(fields, profile, "statusFieldId")
    assignee_field = _semantic_field(fields, profile, "assigneeFieldId")
    priority_field = _semantic_field(fields, profile, "priorityFieldId")
    due_field = _semantic_field(fields, profile, "dueDateFieldId")
    changed_at = await latest_record_change_at(
        db,
        table_id=table.id,
        record_id=record_model.id,
    )

    return {
        "task_id": record_model.id,
        "record_id": record_model.id,
        "target_table_id": table.id,
        "workspace_id": table.workspace_id,
        "title": _scalar_display(title_field, record),
        "status": _scalar_display(status_field, record),
        "assignee": await _assignee_display(db, table.id, assignee_field, record),
        "priority": _scalar_display(priority_field, record),
        "due_date": _scalar_display(due_field, record),
        "updated_at": changed_at.isoformat() if changed_at else None,
        "record_version": max(1, int(record_model.version or 1)),
        "qtable_url": qtable_url,
    }
