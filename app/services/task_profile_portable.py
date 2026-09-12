from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField
from app.services.task_profile import (
    PROFILE_FIELD_KEYS,
    TaskProfileValidationError,
    get_task_profile_record,
    inspect_task_profile_config,
    save_task_profile,
)


ROLE_BY_KEY = {
    "titleFieldId": "title",
    "statusFieldId": "status",
    "assigneeFieldId": "assignee",
    "priorityFieldId": "priority",
    "startDateFieldId": "startDate",
    "dueDateFieldId": "dueDate",
    "progressFieldId": "progress",
    "parentFieldId": "parent",
    "dependencyFieldId": "dependency",
    "workloadFieldId": "workload",
}
KEY_BY_ROLE = {value: key for key, value in ROLE_BY_KEY.items()}
ROLE_PROPERTY = "taskProfileRole"
COMPLETED_PROPERTY = "taskProfileCompletedValues"
BLOCKED_PROPERTY = "taskProfileBlockedValues"
NOT_STARTED_PROPERTY = "taskProfileNotStartedValues"


def _field_payload(field: TableField) -> Dict[str, Any]:
    return {
        "id": str(field.id),
        "tableId": str(field.table_id),
        "name": field.name,
        "type": field.type,
        "options": field.options,
        "property": field.property,
    }


async def _fields(db: AsyncSession, table_id: str) -> list[TableField]:
    result = await db.execute(
        select(TableField)
        .where(TableField.table_id == table_id)
        .order_by(TableField.order_index.asc(), TableField.id.asc())
    )
    return list(result.scalars().all())


async def sync_profile_annotations(
    db: AsyncSession,
    *,
    table_id: str,
    config: Mapping[str, Any],
) -> None:
    """Mirror a saved profile into portable field metadata.

    TableTaskProfile remains the runtime source of truth. These annotations are
    deliberately redundant transport metadata so table copy and template
    snapshots can carry semantics without needing a second field-ID remapper.
    """
    fields = await _fields(db, table_id)
    by_id = {str(field.id): field for field in fields}

    # Remove old transport annotations first so remapping a role cannot leave
    # two fields claiming the same semantic meaning.
    for field in fields:
        prop = dict(field.property or {})
        prop.pop(ROLE_PROPERTY, None)
        prop.pop(COMPLETED_PROPERTY, None)
        prop.pop(BLOCKED_PROPERTY, None)
        prop.pop(NOT_STARTED_PROPERTY, None)
        field.property = prop or None

    for key in PROFILE_FIELD_KEYS:
        field_id = config.get(key)
        if not field_id:
            continue
        field = by_id.get(str(field_id))
        if field is None:
            raise TaskProfileValidationError(f"Mapped field no longer exists: {field_id}")
        prop = dict(field.property or {})
        prop[ROLE_PROPERTY] = ROLE_BY_KEY[key]
        if key == "statusFieldId":
            prop[COMPLETED_PROPERTY] = list(config.get("completedStatusValues") or [])
            prop[BLOCKED_PROPERTY] = list(config.get("blockedStatusValues") or [])
            prop[NOT_STARTED_PROPERTY] = list(config.get("notStartedStatusValues") or [])
        field.property = prop
    await db.flush()


async def clear_profile_annotations(db: AsyncSession, *, table_id: str) -> None:
    for field in await _fields(db, table_id):
        prop = dict(field.property or {})
        changed = False
        for key in (
            ROLE_PROPERTY,
            COMPLETED_PROPERTY,
            BLOCKED_PROPERTY,
            NOT_STARTED_PROPERTY,
        ):
            if key in prop:
                prop.pop(key, None)
                changed = True
        if changed:
            field.property = prop or None
    await db.flush()


def config_from_annotated_fields(fields: list[TableField], *, table_id: str) -> Optional[Dict[str, Any]]:
    config: Dict[str, Any] = {
        "schemaVersion": 1,
        **{key: None for key in PROFILE_FIELD_KEYS},
        "completedStatusValues": [],
        "blockedStatusValues": [],
        "notStartedStatusValues": [],
    }
    found = False
    claimed: set[str] = set()
    for field in fields:
        prop = field.property if isinstance(field.property, Mapping) else {}
        role = prop.get(ROLE_PROPERTY)
        if not role:
            continue
        key = KEY_BY_ROLE.get(str(role))
        if key is None:
            raise TaskProfileValidationError(f"Unknown portable task profile role: {role}")
        if key in claimed:
            raise TaskProfileValidationError(f"Duplicate portable task profile role: {role}")
        claimed.add(key)
        found = True
        config[key] = str(field.id)
        if key == "statusFieldId":
            completed = prop.get(COMPLETED_PROPERTY) or []
            blocked = prop.get(BLOCKED_PROPERTY) or []
            not_started = prop.get(NOT_STARTED_PROPERTY) or []
            if not all(
                isinstance(values, list)
                for values in (completed, blocked, not_started)
            ):
                raise TaskProfileValidationError("Portable status semantics must be lists")
            config["completedStatusValues"] = list(completed)
            config["blockedStatusValues"] = list(blocked)
            config["notStartedStatusValues"] = list(not_started)

    if not found:
        return None
    inspected = inspect_task_profile_config(
        config,
        [_field_payload(field) for field in fields],
        table_id=table_id,
    )
    if not inspected["valid"]:
        message = "; ".join(issue["message"] for issue in inspected["issues"])
        raise TaskProfileValidationError(message)
    return inspected["config"]


async def install_profile_from_annotations(
    db: AsyncSession,
    *,
    table_id: str,
    user_id: Optional[int] = None,
    commit: bool = True,
) -> Optional[Dict[str, Any]]:
    """Materialize portable template/copy metadata into the canonical profile."""
    existing = await get_task_profile_record(db, table_id)
    if existing is not None:
        return None
    fields = await _fields(db, table_id)
    config = config_from_annotated_fields(fields, table_id=table_id)
    if config is None:
        return None
    return await save_task_profile(
        db,
        table_id=table_id,
        config=config,
        user_id=user_id,
        commit=commit,
    )
