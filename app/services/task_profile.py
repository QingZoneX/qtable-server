from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableField, WorkspaceItem
from app.models.task_profile import TableTaskProfile
from app.services.relation_engine import (
    get_target_table_id,
    relation_allows_multiple,
)


class TaskProfileValidationError(ValueError):
    pass


PROFILE_FIELD_KEYS: tuple[str, ...] = (
    "titleFieldId",
    "statusFieldId",
    "assigneeFieldId",
    "priorityFieldId",
    "startDateFieldId",
    "dueDateFieldId",
    "progressFieldId",
    "parentFieldId",
    "dependencyFieldId",
    "workloadFieldId",
)
PROFILE_VALUE_KEYS: tuple[str, ...] = (
    "completedStatusValues",
    "blockedStatusValues",
    "notStartedStatusValues",
)
ALLOWED_PROFILE_KEYS = set(PROFILE_FIELD_KEYS) | set(PROFILE_VALUE_KEYS) | {"schemaVersion"}

# Keep compatibility intentionally broad where QTable already has multiple
# display-oriented text/numeric field variants, while enforcing the semantic
# boundaries that matter to My Work / Kanban / AI consumers.
TEXT_TYPES = {"text", "long_text", "textarea", "email", "url", "formula"}
STATUS_TYPES = {"select", "single_select"}
ASSIGNEE_TYPES = {"member"}
PRIORITY_TYPES = {"select", "single_select", "rating", "number"}
DATE_TYPES = {"date", "datetime"}
PROGRESS_TYPES = {"progress", "number", "rating"}
RELATION_TYPES = {"relation"}
WORKLOAD_TYPES = {"number", "progress", "rating", "duration"}

TYPE_RULES: Dict[str, set[str]] = {
    "titleFieldId": TEXT_TYPES,
    "statusFieldId": STATUS_TYPES,
    "assigneeFieldId": ASSIGNEE_TYPES,
    "priorityFieldId": PRIORITY_TYPES,
    "startDateFieldId": DATE_TYPES,
    "dueDateFieldId": DATE_TYPES,
    "progressFieldId": PROGRESS_TYPES,
    "parentFieldId": RELATION_TYPES,
    "dependencyFieldId": RELATION_TYPES,
    "workloadFieldId": WORKLOAD_TYPES,
}

SUGGESTION_ALIASES: Dict[str, Sequence[str]] = {
    "titleFieldId": ("任务名称", "任务", "标题", "名称", "title", "task", "name"),
    "statusFieldId": ("状态", "status", "stage"),
    "assigneeFieldId": ("负责人", "执行人", "成员", "assignee", "owner", "member"),
    "priorityFieldId": ("优先级", "priority"),
    "startDateFieldId": ("开始时间", "开始日期", "start", "start date"),
    "dueDateFieldId": ("截止日期", "结束时间", "结束日期", "due", "deadline", "end date"),
    "progressFieldId": ("进度", "progress"),
    "parentFieldId": ("父任务", "上级任务", "parent"),
    "dependencyFieldId": ("依赖", "前置任务", "dependency", "depends on"),
    "workloadFieldId": ("工作量", "工时", "workload", "effort", "hours"),
}


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _field_to_dict(field: TableField) -> Dict[str, Any]:
    return {
        "id": str(field.id),
        "tableId": str(field.table_id),
        "name": field.name,
        "type": str(field.type or "").lower(),
        "options": _clone(field.options),
        "property": _clone(field.property),
    }


async def _table_fields(db: AsyncSession, table_id: str) -> List[TableField]:
    result = await db.execute(
        select(TableField)
        .where(TableField.table_id == table_id)
        .order_by(TableField.order_index.asc(), TableField.id.asc())
    )
    return list(result.scalars().all())


async def _require_table(db: AsyncSession, table_id: str) -> WorkspaceItem:
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == table_id,
            WorkspaceItem.type == "table",
        )
    )
    table = result.scalars().first()
    if table is None:
        raise TaskProfileValidationError("Table not found")
    return table


def normalize_task_profile_config(raw: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TaskProfileValidationError("Task profile must be an object")
    unknown = set(raw.keys()) - ALLOWED_PROFILE_KEYS
    if unknown:
        raise TaskProfileValidationError(
            f"Unsupported task profile keys: {', '.join(sorted(str(key) for key in unknown))}"
        )

    normalized: Dict[str, Any] = {"schemaVersion": 1}
    for key in PROFILE_FIELD_KEYS:
        value = raw.get(key)
        if value is None or value == "":
            normalized[key] = None
        elif isinstance(value, str):
            normalized[key] = value.strip() or None
        else:
            raise TaskProfileValidationError(f"{key} must be a field id string or null")

    for key in PROFILE_VALUE_KEYS:
        value = raw.get(key) or []
        if not isinstance(value, list):
            raise TaskProfileValidationError(f"{key} must be a list")
        cleaned: List[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise TaskProfileValidationError(f"{key} values must be scalar option values")
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                cleaned.append(text)
        normalized[key] = cleaned
    return normalized


def _select_values(field: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    options = field.get("options")
    if not isinstance(options, list):
        return values
    for option in options:
        if isinstance(option, Mapping):
            for key in ("id", "value", "label", "name"):
                value = option.get(key)
                if value is not None and str(value).strip():
                    values.add(str(value).strip())
        elif option is not None:
            values.add(str(option).strip())
    return values


def inspect_task_profile_config(
    config: Mapping[str, Any],
    fields: Sequence[Mapping[str, Any]],
    *,
    table_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized = normalize_task_profile_config(config)
    field_map = {str(field.get("id")): field for field in fields if field.get("id") is not None}
    issues: List[Dict[str, Any]] = []

    def add_issue(key: str, code: str, message: str, field_id: Optional[str]) -> None:
        issues.append({
            "key": key,
            "code": code,
            "message": message,
            "fieldId": field_id,
        })

    for key in PROFILE_FIELD_KEYS:
        field_id = normalized.get(key)
        if not field_id:
            continue
        field = field_map.get(str(field_id))
        if field is None:
            add_issue(key, "FIELD_MISSING", f"Mapped field no longer exists: {field_id}", str(field_id))
            continue
        actual_type = str(field.get("type") or "").lower()
        allowed = TYPE_RULES[key]
        if actual_type not in allowed:
            add_issue(
                key,
                "FIELD_TYPE_MISMATCH",
                f"{key} does not accept field type '{actual_type}'",
                str(field_id),
            )
            continue

        if key in {"parentFieldId", "dependencyFieldId"}:
            target = get_target_table_id(field)
            if table_id and target and target not in {table_id, "$SELF"}:
                add_issue(
                    key,
                    "RELATION_TARGET_MISMATCH",
                    f"{key} must point to the same table",
                    str(field_id),
                )
            multiple = relation_allows_multiple(field)
            if key == "parentFieldId" and multiple:
                add_issue(
                    key,
                    "RELATION_CARDINALITY_MISMATCH",
                    "parentFieldId must be a single-value relation",
                    str(field_id),
                )
            if key == "dependencyFieldId" and not multiple:
                add_issue(
                    key,
                    "RELATION_CARDINALITY_MISMATCH",
                    "dependencyFieldId must allow multiple values",
                    str(field_id),
                )

    status_id = normalized.get("statusFieldId")
    status_field = field_map.get(str(status_id)) if status_id else None
    allowed_status_values = _select_values(status_field or {})
    for key in PROFILE_VALUE_KEYS:
        values = normalized.get(key) or []
        if values and not status_id:
            add_issue(key, "STATUS_FIELD_REQUIRED", f"{key} requires statusFieldId", None)
            continue
        if values and status_field is not None and allowed_status_values:
            invalid = [value for value in values if value not in allowed_status_values]
            if invalid:
                add_issue(
                    key,
                    "STATUS_VALUE_INVALID",
                    f"Unknown status values: {', '.join(invalid)}",
                    str(status_id),
                )

    semantic_sets = {
        key: set(normalized.get(key) or [])
        for key in PROFILE_VALUE_KEYS
    }
    for left, right in (
        ("completedStatusValues", "blockedStatusValues"),
        ("completedStatusValues", "notStartedStatusValues"),
        ("blockedStatusValues", "notStartedStatusValues"),
    ):
        overlap = sorted(semantic_sets[left] & semantic_sets[right])
        if overlap:
            add_issue(
                right,
                "STATUS_VALUE_OVERLAP",
                (
                    f"Status values cannot belong to both {left} and {right}: "
                    f"{', '.join(overlap)}"
                ),
                str(status_id) if status_id else None,
            )

    return {
        "config": normalized,
        "valid": not issues,
        "issues": issues,
    }


def validate_task_profile_snapshot(
    raw_profile: Mapping[str, Any],
    fields: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    inspected = inspect_task_profile_config(raw_profile, fields, table_id="$SELF")
    if not inspected["valid"]:
        message = "; ".join(issue["message"] for issue in inspected["issues"])
        raise TaskProfileValidationError(message)
    return inspected["config"]


async def get_task_profile_record(
    db: AsyncSession,
    table_id: str,
) -> Optional[TableTaskProfile]:
    result = await db.execute(
        select(TableTaskProfile).where(TableTaskProfile.table_id == table_id)
    )
    return result.scalars().first()


async def serialize_task_profile(
    db: AsyncSession,
    table_id: str,
) -> Optional[Dict[str, Any]]:
    await _require_table(db, table_id)
    profile = await get_task_profile_record(db, table_id)
    if profile is None:
        return None
    fields = [_field_to_dict(field) for field in await _table_fields(db, table_id)]
    inspected = inspect_task_profile_config(profile.config or {}, fields, table_id=table_id)
    return {
        "tableId": table_id,
        "version": int(profile.version or 1),
        "config": inspected["config"],
        "valid": inspected["valid"],
        "issues": inspected["issues"],
        "updatedByUserId": profile.updated_by_user_id,
        "createdAt": profile.created_at.isoformat() if profile.created_at else None,
        "updatedAt": profile.updated_at.isoformat() if profile.updated_at else None,
    }


async def save_task_profile(
    db: AsyncSession,
    *,
    table_id: str,
    config: Mapping[str, Any],
    user_id: Optional[int],
    commit: bool = True,
) -> Dict[str, Any]:
    await _require_table(db, table_id)
    fields = [_field_to_dict(field) for field in await _table_fields(db, table_id)]
    inspected = inspect_task_profile_config(config, fields, table_id=table_id)
    if not inspected["valid"]:
        message = "; ".join(issue["message"] for issue in inspected["issues"])
        raise TaskProfileValidationError(message)

    existing = await get_task_profile_record(db, table_id)
    if existing is None:
        existing = TableTaskProfile(
            table_id=table_id,
            config=inspected["config"],
            version=1,
            updated_by_user_id=user_id,
        )
        db.add(existing)
    else:
        existing.config = inspected["config"]
        existing.version = int(existing.version or 0) + 1
        existing.updated_by_user_id = user_id
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(existing)
    payload = await serialize_task_profile(db, table_id)
    assert payload is not None
    return payload


async def clear_task_profile(
    db: AsyncSession,
    *,
    table_id: str,
    commit: bool = True,
) -> bool:
    await _require_table(db, table_id)
    profile = await get_task_profile_record(db, table_id)
    if profile is None:
        return False
    await db.delete(profile)
    await db.flush()
    if commit:
        await db.commit()
    return True


async def copy_task_profile(
    db: AsyncSession,
    *,
    source_table_id: str,
    target_table_id: str,
    user_id: Optional[int] = None,
    commit: bool = True,
) -> Optional[Dict[str, Any]]:
    source = await get_task_profile_record(db, source_table_id)
    if source is None:
        return None
    return await save_task_profile(
        db,
        table_id=target_table_id,
        config=_clone(source.config or {}),
        user_id=user_id,
        commit=commit,
    )


def _normalized_name(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r"[\s_\-]+", " ", text)


def suggest_task_profile_config(fields: Sequence[Mapping[str, Any]], *, table_id: Optional[str] = None) -> Dict[str, Any]:
    """Return a deterministic suggestion only; never persist it automatically.

    Name matching exists solely in this one-time migration assistant. Runtime
    consumers must use an explicit saved profile and never repeat this guessing.
    """
    selected: Dict[str, Optional[str]] = {key: None for key in PROFILE_FIELD_KEYS}
    confidence: Dict[str, float] = {}
    reasons: Dict[str, str] = {}

    for key in PROFILE_FIELD_KEYS:
        allowed = TYPE_RULES[key]
        aliases = [_normalized_name(alias) for alias in SUGGESTION_ALIASES.get(key, ())]
        candidates: List[tuple[float, Mapping[str, Any]]] = []
        for field in fields:
            field_type = str(field.get("type") or "").lower()
            if field_type not in allowed:
                continue
            name = _normalized_name(field.get("name"))
            score = 0.20
            if name in aliases:
                score = 0.98
            elif any(alias and alias in name for alias in aliases):
                score = 0.82
            elif field_type in ({"member"} if key == "assigneeFieldId" else set()):
                score = 0.60
            if key in {"parentFieldId", "dependencyFieldId"}:
                target = get_target_table_id(field)
                if table_id and target not in {table_id, "$SELF"}:
                    continue
                multiple = relation_allows_multiple(field)
                if key == "parentFieldId" and multiple:
                    continue
                if key == "dependencyFieldId" and not multiple:
                    continue
            candidates.append((score, field))
        if not candidates:
            continue
        candidates.sort(key=lambda item: (-item[0], str(item[1].get("id"))))
        score, field = candidates[0]
        # Avoid pretending low-information type-only guesses are reliable for
        # ambiguous numeric/date/text fields.
        if score < 0.50:
            continue
        field_id = str(field.get("id"))
        selected[key] = field_id
        confidence[key] = round(score, 2)
        reasons[key] = f"Matched '{field.get('name')}' ({field.get('type')})"

    completed: List[str] = []
    blocked: List[str] = []
    not_started: List[str] = []
    status_id = selected.get("statusFieldId")
    status = next((field for field in fields if str(field.get("id")) == str(status_id)), None)
    if status:
        for option in status.get("options") or []:
            if not isinstance(option, Mapping):
                continue
            value = option.get("id") or option.get("value") or option.get("label")
            label = _normalized_name(option.get("label") or option.get("name") or value)
            if value is None:
                continue
            if any(token in label for token in ("完成", "done", "completed", "closed")):
                completed.append(str(value))
            if any(token in label for token in ("阻塞", "blocked", "block")):
                blocked.append(str(value))
            if any(
                token in label
                for token in (
                    "未开始",
                    "待开始",
                    "todo",
                    "not started",
                    "backlog",
                    "planned",
                )
            ):
                not_started.append(str(value))

    config: Dict[str, Any] = {"schemaVersion": 1, **selected}
    config["completedStatusValues"] = completed
    config["blockedStatusValues"] = blocked
    config["notStartedStatusValues"] = not_started
    return {
        "config": config,
        "confidence": confidence,
        "reasons": reasons,
        "requiresConfirmation": True,
    }


async def suggest_task_profile(db: AsyncSession, table_id: str) -> Dict[str, Any]:
    await _require_table(db, table_id)
    fields = [_field_to_dict(field) for field in await _table_fields(db, table_id)]
    return suggest_task_profile_config(fields, table_id=table_id)
