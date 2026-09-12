from __future__ import annotations

import base64
import copy
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_history import ChangeItem, ChangeSet
from app.models.my_work import MyWorkRecentTarget
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    TableView,
    WorkspaceItem,
    WorkspaceItemPermission,
)
from app.models.task_profile import TableTaskProfile
from app.models.user import User
from app.models.workspace_member import WorkspaceMember
from app.services.row_permissions import record_is_visible, require_record_access
from app.services.task_profile import inspect_task_profile_config
from app.services.workspace import (
    get_effective_permission_for_item,
    normalize_permission,
    permission_allows,
    role_permission,
)

logger = logging.getLogger(__name__)

DEFAULT_SECTIONS = ("tasks", "due", "projects", "recent", "activity", "kpi")
ALLOWED_SECTIONS = set(DEFAULT_SECTIONS)
ALLOWED_TASK_STATES = {"all", "in_progress", "not_started", "completed"}
MAX_LIMIT = 100
MAX_CURSOR_OFFSET = 5000
ACTIVITY_CURSOR_MAX = 100000
RECENT_RETENTION = 50
ACTIVITY_SCAN_CAP = 2000


class MyWorkValidationError(ValueError):
    pass


@dataclass
class TaskTableContext:
    table: WorkspaceItem
    profile: Optional[TableTaskProfile]
    config: Dict[str, Any]
    fields: Dict[str, TableField]
    permission: str
    row_policy: Dict[str, Any]

    @property
    def table_id(self) -> str:
        return str(self.table.id)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MyWorkValidationError("limit must be an integer") from exc
    return max(1, min(MAX_LIMIT, parsed))


def _timezone(value: str) -> ZoneInfo:
    name = (value or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise MyWorkValidationError(f"Unknown timezone: {name}") from exc


def _encode_cursor(offset: int) -> str:
    payload = json.dumps({"o": max(0, int(offset))}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(
    value: Optional[str],
    *,
    max_offset: int = MAX_CURSOR_OFFSET,
) -> int:
    if not value:
        return 0
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset = int(payload.get("o", 0))
    except Exception as exc:
        raise MyWorkValidationError("Invalid cursor") from exc
    if offset < 0 or offset > max_offset:
        raise MyWorkValidationError("Cursor is outside the supported window")
    return offset


def _cursor_for(cursors: Optional[Mapping[str, Any]], section: str) -> int:
    if not isinstance(cursors, Mapping):
        return 0
    raw = cursors.get(section)
    max_offset = (
        ACTIVITY_CURSOR_MAX
        if section == "activity"
        else MAX_CURSOR_OFFSET
    )
    return (
        _decode_cursor(str(raw), max_offset=max_offset)
        if raw
        else 0
    )


def _page_info(offset: int, returned: int, has_more: bool) -> Dict[str, Any]:
    return {
        "offset": offset,
        "hasMore": bool(has_more),
        "nextCursor": _encode_cursor(offset + returned) if has_more else None,
    }


def _field_payload(field: TableField) -> Dict[str, Any]:
    return {
        "id": str(field.id),
        "tableId": str(field.table_id),
        "name": field.name,
        "type": str(field.type or "").lower(),
        "options": copy.deepcopy(field.options),
        "property": copy.deepcopy(field.property),
    }


def _policy_payload(policy: Optional[TableRowPermissionPolicy]) -> Dict[str, Any]:
    if policy is None:
        return {"mode": "all", "memberFieldId": None, "enabled": False}
    mode = str(policy.mode or "all")
    if mode not in {"all", "creator", "member_field"}:
        # Stale/unknown policy modes fail closed for non-managers later.
        mode = "__invalid__"
    field_id = (
        str(policy.member_field_id)
        if mode == "member_field" and policy.member_field_id
        else None
    )
    return {
        "mode": mode,
        "memberFieldId": field_id,
        "enabled": mode != "all",
    }


async def _effective_permissions_for_tables(
    db: AsyncSession,
    *,
    user_id: int,
    tables: Sequence[WorkspaceItem],
) -> Dict[str, Optional[str]]:
    """Resolve workspace/item inheritance in batches for workbench aggregation."""
    if not tables:
        return {}
    workspace_ids = {str(item.workspace_id) for item in tables}
    all_items_result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.workspace_id.in_(list(workspace_ids))
        )
    )
    all_items = list(all_items_result.scalars().all())
    parent_by_id = {
        str(item.id): (str(item.parent_id) if item.parent_id else None)
        for item in all_items
    }
    all_item_ids = list(parent_by_id.keys())

    member_result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id.in_(list(workspace_ids)),
        )
    )
    membership_by_workspace = {
        str(member.workspace_id): member
        for member in member_result.scalars().all()
    }

    overrides: Dict[str, str] = {}
    if all_item_ids:
        override_result = await db.execute(
            select(WorkspaceItemPermission).where(
                WorkspaceItemPermission.user_id == user_id,
                WorkspaceItemPermission.item_id.in_(all_item_ids),
            )
        )
        overrides = {
            str(row.item_id): normalize_permission(row.permission)
            for row in override_result.scalars().all()
        }

    resolved: Dict[str, Optional[str]] = {}
    for table in tables:
        table_id = str(table.id)
        member = membership_by_workspace.get(str(table.workspace_id))
        if member is None:
            resolved[table_id] = None
            continue
        permission = role_permission(member.role)
        current: Optional[str] = table_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            if current in overrides:
                permission = overrides[current]
                break
            current = parent_by_id.get(current)
        resolved[table_id] = permission
    return resolved


async def _task_table_contexts(
    db: AsyncSession,
    *,
    user_id: int,
) -> List[TaskTableContext]:
    result = await db.execute(
        select(WorkspaceItem, TableTaskProfile)
        .join(TableTaskProfile, TableTaskProfile.table_id == WorkspaceItem.id)
        .where(WorkspaceItem.type == "table")
        .order_by(WorkspaceItem.workspace_id.asc(), WorkspaceItem.id.asc())
    )
    pairs = list(result.all())
    if not pairs:
        return []

    table_ids = [str(item.id) for item, _ in pairs]
    fields_result = await db.execute(
        select(TableField)
        .where(TableField.table_id.in_(table_ids))
        .order_by(TableField.table_id.asc(), TableField.order_index.asc())
    )
    fields_by_table: Dict[str, List[TableField]] = {}
    for field in fields_result.scalars().all():
        fields_by_table.setdefault(str(field.table_id), []).append(field)

    policy_result = await db.execute(
        select(TableRowPermissionPolicy).where(
            TableRowPermissionPolicy.table_id.in_(table_ids)
        )
    )
    policy_by_table = {
        str(policy.table_id): policy for policy in policy_result.scalars().all()
    }

    permission_by_table = await _effective_permissions_for_tables(
        db,
        user_id=user_id,
        tables=[item for item, _ in pairs],
    )
    contexts: List[TaskTableContext] = []
    for item, profile in pairs:
        table_id = str(item.id)
        permission = permission_by_table.get(table_id)
        if not permission_allows(permission, "read"):
            continue
        fields = fields_by_table.get(table_id, [])
        inspected = inspect_task_profile_config(
            profile.config or {},
            [_field_payload(field) for field in fields],
            table_id=table_id,
        )
        if not inspected["valid"]:
            # Invalid mappings must never fall back to field-name guessing.
            continue
        contexts.append(
            TaskTableContext(
                table=item,
                profile=profile,
                config=dict(inspected["config"]),
                fields={str(field.id): field for field in fields},
                permission=str(permission),
                row_policy=_policy_payload(policy_by_table.get(table_id)),
            )
        )
    return contexts


async def _activity_table_contexts(
    db: AsyncSession,
    *,
    user_id: int,
) -> List[TaskTableContext]:
    """Load every readable table for activity without requiring Task Profile.

    Generic multidimensional tables still produce record-created/updated
    activity. When a valid Task Profile exists it enriches the same activity
    with title/status/assignee semantics. Invalid profiles are ignored here
    rather than guessed or allowed to hide otherwise-readable history.
    """
    table_result = await db.execute(
        select(WorkspaceItem)
        .where(WorkspaceItem.type == "table")
        .order_by(WorkspaceItem.workspace_id.asc(), WorkspaceItem.id.asc())
    )
    tables = list(table_result.scalars().all())
    if not tables:
        return []
    table_ids = [str(item.id) for item in tables]

    profile_result = await db.execute(
        select(TableTaskProfile).where(TableTaskProfile.table_id.in_(table_ids))
    )
    profiles = {
        str(profile.table_id): profile
        for profile in profile_result.scalars().all()
    }
    fields_result = await db.execute(
        select(TableField)
        .where(TableField.table_id.in_(table_ids))
        .order_by(TableField.table_id.asc(), TableField.order_index.asc())
    )
    fields_by_table: Dict[str, List[TableField]] = {}
    for field in fields_result.scalars().all():
        fields_by_table.setdefault(str(field.table_id), []).append(field)
    policy_result = await db.execute(
        select(TableRowPermissionPolicy).where(
            TableRowPermissionPolicy.table_id.in_(table_ids)
        )
    )
    policies = {
        str(policy.table_id): policy
        for policy in policy_result.scalars().all()
    }

    permission_by_table = await _effective_permissions_for_tables(
        db,
        user_id=user_id,
        tables=tables,
    )
    contexts: List[TaskTableContext] = []
    for item in tables:
        table_id = str(item.id)
        permission = permission_by_table.get(table_id)
        if not permission_allows(permission, "read"):
            continue
        fields = fields_by_table.get(table_id, [])
        profile = profiles.get(table_id)
        config: Dict[str, Any] = {}
        if profile is not None:
            inspected = inspect_task_profile_config(
                profile.config or {},
                [_field_payload(field) for field in fields],
                table_id=table_id,
            )
            if inspected["valid"]:
                config = dict(inspected["config"])
        contexts.append(
            TaskTableContext(
                table=item,
                profile=profile,
                config=config,
                fields={str(field.id): field for field in fields},
                permission=str(permission),
                row_policy=_policy_payload(policies.get(table_id)),
            )
        )
    return contexts


def _json_path(field_id: str) -> str:
    escaped = str(field_id).replace("\\", "\\\\").replace('"', '\\"')
    return f'$."{escaped}"'


def _dialect_name(db: AsyncSession) -> str:
    return str(db.get_bind().dialect.name)


def _json_text_expr(dialect: str, alias: str, key_param: str, path_param: str) -> str:
    if dialect == "postgresql":
        return f"(CAST({alias}.data AS jsonb) ->> :{key_param})"
    return f"CAST(json_extract({alias}.data, :{path_param}) AS TEXT)"


def _json_text_expr_for_column(
    dialect: str,
    column_sql: str,
    key_param: str,
    path_param: str,
) -> str:
    if dialect == "postgresql":
        return f"(CAST({column_sql} AS jsonb) ->> :{key_param})"
    return f"CAST(json_extract({column_sql}, :{path_param}) AS TEXT)"


def _member_match_sql(
    dialect: str,
    *,
    alias: str,
    field_key_param: str,
    field_path_param: str,
    user_param: str,
    array_param: str,
) -> str:
    if dialect == "postgresql":
        return (
            f"((CAST({alias}.data AS jsonb) ->> :{field_key_param}) = :{user_param} "
            f"OR (CAST({alias}.data AS jsonb) -> :{field_key_param}) "
            f"@> CAST(:{array_param} AS jsonb))"
        )
    return (
        f"EXISTS (SELECT 1 FROM json_each({alias}.data, :{field_path_param}) AS member_value "
        f"WHERE CAST(member_value.value AS TEXT) = :{user_param})"
    )


def _member_match_sql_for_column(
    dialect: str,
    *,
    column_sql: str,
    field_key_param: str,
    field_path_param: str,
    user_param: str,
    array_param: str,
) -> str:
    if dialect == "postgresql":
        return (
            f"((CAST({column_sql} AS jsonb) ->> :{field_key_param}) = :{user_param} "
            f"OR (CAST({column_sql} AS jsonb) -> :{field_key_param}) "
            f"@> CAST(:{array_param} AS jsonb))"
        )
    return (
        f"EXISTS (SELECT 1 FROM json_each({column_sql}, :{field_path_param}) AS member_value "
        f"WHERE CAST(member_value.value AS TEXT) = :{user_param})"
    )


def _visibility_sql(
    ctx: TaskTableContext,
    dialect: str,
    params: Dict[str, Any],
    *,
    alias: str = "r",
) -> str:
    if permission_allows(ctx.permission, "manage"):
        return "1=1"
    mode = str(ctx.row_policy.get("mode") or "all")
    if mode == "all":
        return "1=1"
    if mode == "creator":
        return f"{alias}.created_by_user_id = :user_id"
    field_id = ctx.row_policy.get("memberFieldId")
    if mode == "member_field" and field_id:
        params["visibility_member_key"] = str(field_id)
        params["visibility_member_path"] = _json_path(str(field_id))
        params["visibility_member_user"] = str(params["user_id"])
        params["visibility_member_array"] = json.dumps(
            [str(params["user_id"])], separators=(",", ":")
        )
        member = _member_match_sql(
            dialect,
            alias=alias,
            field_key_param="visibility_member_key",
            field_path_param="visibility_member_path",
            user_param="visibility_member_user",
            array_param="visibility_member_array",
        )
        return f"({alias}.created_by_user_id = :user_id OR {member})"
    return "0=1"


def _assigned_sql(
    ctx: TaskTableContext,
    dialect: str,
    params: Dict[str, Any],
    *,
    alias: str = "r",
) -> Optional[str]:
    field_id = ctx.config.get("assigneeFieldId")
    if not field_id:
        return None
    params["assignee_key"] = str(field_id)
    params["assignee_path"] = _json_path(str(field_id))
    params["assignee_user"] = str(params["user_id"])
    params["assignee_array"] = json.dumps(
        [str(params["user_id"])], separators=(",", ":")
    )
    return _member_match_sql(
        dialect,
        alias=alias,
        field_key_param="assignee_key",
        field_path_param="assignee_path",
        user_param="assignee_user",
        array_param="assignee_array",
    )


def _sql_in(
    expr: str,
    values: Sequence[str],
    params: Dict[str, Any],
    prefix: str,
) -> str:
    placeholders: List[str] = []
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        params[key] = str(value)
        placeholders.append(f":{key}")
    return f"{expr} IN ({', '.join(placeholders)})" if placeholders else "0=1"


def _not_true(condition: str) -> str:
    # Works on PostgreSQL booleans and SQLite's 0/1 boolean representation,
    # while treating NULL status values as not completed/not-started.
    return f"COALESCE(({condition}), FALSE) = FALSE"


def _completion_sql(
    ctx: TaskTableContext,
    dialect: str,
    params: Dict[str, Any],
    *,
    alias: str = "r",
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    status_field = ctx.config.get("statusFieldId")
    if not status_field:
        return None, None, None
    params["status_key"] = str(status_field)
    params["status_path"] = _json_path(str(status_field))
    expr = _json_text_expr(dialect, alias, "status_key", "status_path")
    completed = [str(value) for value in ctx.config.get("completedStatusValues") or []]
    not_started = [
        str(value) for value in ctx.config.get("notStartedStatusValues") or []
    ]
    blocked = [str(value) for value in ctx.config.get("blockedStatusValues") or []]
    completed_sql = _sql_in(expr, completed, params, "completed") if completed else None
    not_started_sql = _sql_in(expr, not_started, params, "not_started") if not_started else None
    blocked_sql = _sql_in(expr, blocked, params, "blocked") if blocked else None
    return completed_sql, not_started_sql, blocked_sql


def _task_state_condition(
    ctx: TaskTableContext,
    dialect: str,
    params: Dict[str, Any],
    task_state: str,
    *,
    alias: str = "r",
) -> Optional[str]:
    if task_state == "all":
        return None
    completed_sql, not_started_sql, _ = _completion_sql(
        ctx, dialect, params, alias=alias
    )
    if task_state == "completed":
        return completed_sql or "0=1"
    if task_state == "not_started":
        return not_started_sql or "0=1"
    excluded = [value for value in (completed_sql, not_started_sql) if value]
    if not excluded:
        return None
    return " AND ".join(_not_true(value) for value in excluded)


def _select_exprs(
    ctx: TaskTableContext,
    dialect: str,
    params: Dict[str, Any],
    *,
    alias: str = "r",
) -> Dict[str, str]:
    result: Dict[str, str] = {}
    mapping = {
        "title": "titleFieldId",
        "status": "statusFieldId",
        "priority": "priorityFieldId",
        "start": "startDateFieldId",
        "due": "dueDateFieldId",
        "progress": "progressFieldId",
    }
    for short, config_key in mapping.items():
        field_id = ctx.config.get(config_key)
        if not field_id:
            result[short] = "NULL"
            continue
        key_param = f"{short}_key"
        path_param = f"{short}_path"
        params[key_param] = str(field_id)
        params[path_param] = _json_path(str(field_id))
        result[short] = _json_text_expr(
            dialect,
            alias,
            key_param,
            path_param,
        )
    return result


async def _fetch_assigned_rows(
    db: AsyncSession,
    *,
    ctx: TaskTableContext,
    user_id: int,
    task_state: str,
    fetch_limit: int,
) -> tuple[List[Mapping[str, Any]], int]:
    dialect = _dialect_name(db)
    params: Dict[str, Any] = {
        "table_id": ctx.table_id,
        "user_id": int(user_id),
        "fetch_limit": max(
            1,
            min(MAX_CURSOR_OFFSET + MAX_LIMIT + 1, int(fetch_limit)),
        ),
    }
    assigned = _assigned_sql(ctx, dialect, params)
    if not assigned:
        return [], 0
    visibility = _visibility_sql(ctx, dialect, params)
    state_condition = _task_state_condition(
        ctx,
        dialect,
        params,
        task_state,
    )
    exprs = _select_exprs(ctx, dialect, params)
    where_parts = ["r.table_id = :table_id", visibility, assigned]
    if state_condition:
        where_parts.append(state_condition)
    due_expr = exprs["due"]
    priority_expr = exprs["priority"]
    order_parts: List[str] = []
    if ctx.config.get("dueDateFieldId"):
        order_parts.extend(
            [
                (
                    f"CASE WHEN {due_expr} IS NULL OR {due_expr} = '' "
                    "THEN 1 ELSE 0 END ASC"
                ),
                f"{due_expr} ASC",
            ]
        )
    if ctx.config.get("priorityFieldId"):
        order_parts.append(f"{priority_expr} DESC")
    order_parts.append("r.id ASC")
    statement = text(
        f"""
        SELECT
            r.id AS record_id,
            r.data AS data,
            r.created_by_user_id AS created_by_user_id,
            {exprs['title']} AS title_value,
            {exprs['status']} AS status_value,
            {exprs['priority']} AS priority_value,
            {exprs['start']} AS start_value,
            {exprs['due']} AS due_value,
            {exprs['progress']} AS progress_value,
            COUNT(*) OVER() AS total_count
        FROM table_records r
        WHERE {' AND '.join(f'({part})' for part in where_parts)}
        ORDER BY {', '.join(order_parts)}
        LIMIT :fetch_limit
        """
    )
    rows = list((await db.execute(statement, params)).mappings().all())
    total = int(rows[0]["total_count"]) if rows else 0
    return rows, total


def _parse_due(value: Any, tz: ZoneInfo) -> Optional[datetime]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if len(raw) <= 10:
        try:
            parsed_date = date.fromisoformat(raw[:10])
        except ValueError:
            return None
        return datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            23,
            59,
            59,
            999999,
            tzinfo=tz,
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _status_label(ctx: TaskTableContext, value: Any) -> Optional[str]:
    field_id = ctx.config.get("statusFieldId")
    field = ctx.fields.get(str(field_id)) if field_id else None
    if field is None or value is None:
        return str(value) if value is not None else None
    for option in field.options or []:
        if not isinstance(option, Mapping):
            continue
        candidates = (
            option.get("id"),
            option.get("value"),
            option.get("label"),
            option.get("name"),
        )
        if any(
            candidate is not None and str(candidate) == str(value)
            for candidate in candidates
        ):
            return str(option.get("label") or option.get("name") or value)
    return str(value)


def _priority_rank(ctx: TaskTableContext, value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    field_id = ctx.config.get("priorityFieldId")
    field = ctx.fields.get(str(field_id)) if field_id else None
    options = list(field.options or []) if field is not None else []
    for index, option in enumerate(options):
        if not isinstance(option, Mapping):
            continue
        candidates = (
            option.get("id"),
            option.get("value"),
            option.get("label"),
            option.get("name"),
        )
        if any(
            candidate is not None and str(candidate) == str(value)
            for candidate in candidates
        ):
            # Explicit option order is used; field-name/label guessing is not.
            return float(len(options) - index)
    return 0.0


def _progress_value(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _task_bucket(due: Optional[datetime], now_local: datetime) -> str:
    if due is None:
        return "unscheduled"
    if due < now_local:
        return "overdue"
    if due.date() == now_local.date():
        return "today"
    if due <= now_local + timedelta(days=7):
        return "soon"
    return "later"


def _record_deep_link(ctx: TaskTableContext, record_id: str) -> str:
    view_id = str(ctx.table.default_view_id or "v1")
    return (
        f"/workbench/{quote(ctx.table_id, safe='')}/{quote(view_id, safe='')}"
        f"?recordId={quote(str(record_id), safe='')}"
    )


def _table_deep_link(item: WorkspaceItem, view_id: Optional[str] = None) -> str:
    target_view = str(view_id or item.default_view_id or "v1")
    return (
        f"/workbench/{quote(str(item.id), safe='')}/"
        f"{quote(target_view, safe='')}"
    )


def _row_to_task(
    ctx: TaskTableContext,
    row: Mapping[str, Any],
    *,
    tz: ZoneInfo,
    now_local: datetime,
) -> Dict[str, Any]:
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    data = dict(data or {})
    record_id = str(row["record_id"])
    due = _parse_due(row.get("due_value"), tz)
    status = row.get("status_value")
    completed_values = {
        str(value) for value in ctx.config.get("completedStatusValues") or []
    }
    blocked_values = {
        str(value) for value in ctx.config.get("blockedStatusValues") or []
    }
    not_started_values = {
        str(value) for value in ctx.config.get("notStartedStatusValues") or []
    }
    priority = row.get("priority_value")
    assignee_field = ctx.config.get("assigneeFieldId")
    is_completed = status is not None and str(status) in completed_values
    return {
        "recordId": record_id,
        "tableId": ctx.table_id,
        "tableName": ctx.table.name,
        "workspaceId": ctx.table.workspace_id,
        "title": str(row.get("title_value") or record_id),
        "status": status,
        "statusLabel": _status_label(ctx, status),
        "assignees": data.get(str(assignee_field)) if assignee_field else None,
        "priority": priority,
        "priorityRank": _priority_rank(ctx, priority),
        "startAt": row.get("start_value"),
        "dueAt": row.get("due_value"),
        "progress": _progress_value(row.get("progress_value")),
        "isCompleted": is_completed,
        "isBlocked": status is not None and str(status) in blocked_values,
        "isNotStarted": status is not None and str(status) in not_started_values,
        "timingBucket": "completed" if is_completed else _task_bucket(due, now_local),
        "deepLink": _record_deep_link(ctx, record_id),
        "_dueSort": due.timestamp() if due is not None else float("inf"),
    }


def _task_sort_key(task: Mapping[str, Any]) -> tuple[Any, ...]:
    bucket_order = {
        "overdue": 0,
        "today": 1,
        "soon": 2,
        "later": 3,
        "unscheduled": 4,
        "completed": 5,
    }
    return (
        bucket_order.get(str(task.get("timingBucket")), 9),
        float(task.get("_dueSort", float("inf"))),
        -float(task.get("priorityRank") or 0),
        str(task.get("tableId") or ""),
        str(task.get("recordId") or ""),
    )


async def _tasks_section(
    db: AsyncSession,
    *,
    contexts: Sequence[TaskTableContext],
    user_id: int,
    task_state: str,
    limit: int,
    offset: int,
    tz: ZoneInfo,
) -> Dict[str, Any]:
    now_local = _utcnow().astimezone(tz)
    fetch_limit = offset + limit + 1
    items: List[Dict[str, Any]] = []
    total = 0
    skipped: List[Dict[str, Any]] = []
    for ctx in contexts:
        if not ctx.config.get("assigneeFieldId"):
            skipped.append({
                "tableId": ctx.table_id,
                "reason": "assigneeFieldId missing",
            })
            continue
        if (
            task_state in {"completed", "not_started"}
            and not ctx.config.get("statusFieldId")
        ):
            skipped.append({
                "tableId": ctx.table_id,
                "reason": "statusFieldId missing",
            })
            continue
        rows, table_total = await _fetch_assigned_rows(
            db,
            ctx=ctx,
            user_id=user_id,
            task_state=task_state,
            fetch_limit=fetch_limit,
        )
        total += table_total
        items.extend(
            _row_to_task(ctx, row, tz=tz, now_local=now_local)
            for row in rows
        )
    items.sort(key=_task_sort_key)
    page = items[offset : offset + limit]
    for item in page:
        item.pop("_dueSort", None)
    return {
        "items": page,
        "totalCount": total,
        "state": task_state,
        "skippedTables": skipped,
        "pageInfo": _page_info(
            offset,
            len(page),
            offset + len(page) < total,
        ),
    }


def _due_window(
    task: Mapping[str, Any],
    now_local: datetime,
    tz: ZoneInfo,
) -> set[str]:
    due = _parse_due(task.get("dueAt"), tz)
    if due is None or task.get("isCompleted"):
        return set()
    buckets: set[str] = set()
    if due < now_local:
        buckets.add("overdue")
    if due.date() == now_local.date():
        buckets.add("today")
    if now_local <= due <= now_local + timedelta(hours=24):
        buckets.add("next24h")
    if now_local <= due <= now_local + timedelta(days=3):
        buckets.add("next3d")
    if now_local <= due <= now_local + timedelta(days=7):
        buckets.add("next7d")
    return buckets


def _due_bucket_condition(
    *,
    dialect: str,
    due_expr: str,
    bucket: str,
    params: Dict[str, Any],
    now_local: datetime,
) -> str:
    """Build an exact SQL predicate for one due bucket.

    Date-only task fields use end-of-local-day semantics. Timestamp values are
    compared as instants. QTable date fields are expected to contain ISO-8601
    values; malformed legacy values simply do not match SQLite date functions
    and should be repaired at the field/data layer.
    """
    now_utc = now_local.astimezone(timezone.utc)
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(timezone.utc)
    next_day_utc = next_day_local.astimezone(timezone.utc)

    params["due_today"] = now_local.date().isoformat()

    if dialect == "postgresql":
        # asyncpg infers TIMESTAMPTZ from the explicit CAST context and
        # therefore requires real datetime bind values, not ISO strings.
        params["due_now_utc"] = now_utc
        params["due_day_start_utc"] = day_start_utc
        params["due_next_day_utc"] = next_day_utc
        timestamp_expr = f"CAST({due_expr} AS TIMESTAMPTZ)"
        now_expr = "CAST(:due_now_utc AS TIMESTAMPTZ)"
        day_start_expr = "CAST(:due_day_start_utc AS TIMESTAMPTZ)"
        next_day_expr = "CAST(:due_next_day_utc AS TIMESTAMPTZ)"
    else:
        params["due_now_utc"] = now_utc.isoformat()
        params["due_day_start_utc"] = day_start_utc.isoformat()
        params["due_next_day_utc"] = next_day_utc.isoformat()
        timestamp_expr = f"datetime({due_expr})"
        now_expr = "datetime(:due_now_utc)"
        day_start_expr = "datetime(:due_day_start_utc)"
        next_day_expr = "datetime(:due_next_day_utc)"

    non_empty = f"({due_expr} IS NOT NULL AND {due_expr} <> '')"
    date_only = f"(LENGTH({due_expr}) <= 10)"
    # Long strings must look ISO-like (contain at least one non-digit such as
    # '-', 'T', ':', 'Z'). Legacy data may store raw JS epoch milliseconds
    # like "1790611200000"; casting those to TIMESTAMPTZ overflows the year
    # field and aborts the whole aggregate. Such values simply do not match.
    # SQLite's datetime() already returns NULL for unparseable inputs, so it
    # is left alone.
    if dialect == "postgresql":
        timestamp = f"({due_expr} ~ '[^0-9]' AND LENGTH({due_expr}) > 10)"
    else:
        timestamp = f"(LENGTH({due_expr}) > 10)"

    if bucket == "overdue":
        return (
            f"{non_empty} AND ("
            f"({date_only} AND {due_expr} < :due_today) OR "
            f"({timestamp} AND {timestamp_expr} < {now_expr})"
            f")"
        )

    if bucket == "today":
        return (
            f"{non_empty} AND ("
            f"({date_only} AND {due_expr} = :due_today) OR "
            f"({timestamp} AND {timestamp_expr} >= {day_start_expr} "
            f"AND {timestamp_expr} < {next_day_expr})"
            f")"
        )

    days = {"next24h": 1, "next3d": 3, "next7d": 7}.get(bucket)
    if days is None:
        raise MyWorkValidationError(f"Unknown due bucket: {bucket}")
    window_end_local = now_local + timedelta(days=days)
    window_end_utc = window_end_local.astimezone(timezone.utc)
    # Date-only values become due at 23:59:59 local. Only include a date if
    # that end-of-day instant falls inside the requested rolling window.
    candidate_date = window_end_local.date()
    candidate_eod = datetime(
        candidate_date.year,
        candidate_date.month,
        candidate_date.day,
        23,
        59,
        59,
        999999,
        tzinfo=now_local.tzinfo,
    )
    max_date = (
        candidate_date
        if candidate_eod <= window_end_local
        else candidate_date - timedelta(days=1)
    )
    params["due_max_date"] = max_date.isoformat()
    if dialect == "postgresql":
        params["due_window_end_utc"] = window_end_utc
        window_end_expr = "CAST(:due_window_end_utc AS TIMESTAMPTZ)"
    else:
        params["due_window_end_utc"] = window_end_utc.isoformat()
        window_end_expr = "datetime(:due_window_end_utc)"
    return (
        f"{non_empty} AND ("
        f"({date_only} AND {due_expr} >= :due_today "
        f"AND {due_expr} <= :due_max_date) OR "
        f"({timestamp} AND {timestamp_expr} >= {now_expr} "
        f"AND {timestamp_expr} <= {window_end_expr})"
        f")"
    )


async def _fetch_due_bucket_rows(
    db: AsyncSession,
    *,
    ctx: TaskTableContext,
    user_id: int,
    bucket: str,
    fetch_limit: int,
    now_local: datetime,
) -> tuple[List[Mapping[str, Any]], int]:
    dialect = _dialect_name(db)
    params: Dict[str, Any] = {
        "table_id": ctx.table_id,
        "user_id": int(user_id),
        "fetch_limit": max(
            1,
            min(MAX_CURSOR_OFFSET + MAX_LIMIT + 1, int(fetch_limit)),
        ),
    }
    assigned = _assigned_sql(ctx, dialect, params)
    if not assigned or not ctx.config.get("dueDateFieldId"):
        return [], 0
    visibility = _visibility_sql(ctx, dialect, params)
    completed_sql, _, _ = _completion_sql(ctx, dialect, params)
    incomplete = _not_true(completed_sql) if completed_sql else "1=1"
    exprs = _select_exprs(ctx, dialect, params)
    due_expr = exprs["due"]
    due_condition = _due_bucket_condition(
        dialect=dialect,
        due_expr=due_expr,
        bucket=bucket,
        params=params,
        now_local=now_local,
    )
    priority_expr = exprs["priority"]
    order_parts = [f"{due_expr} ASC"]
    if ctx.config.get("priorityFieldId"):
        order_parts.append(f"{priority_expr} DESC")
    order_parts.append("r.id ASC")
    statement = text(
        f"""
        SELECT
            r.id AS record_id,
            r.data AS data,
            r.created_by_user_id AS created_by_user_id,
            {exprs['title']} AS title_value,
            {exprs['status']} AS status_value,
            {exprs['priority']} AS priority_value,
            {exprs['start']} AS start_value,
            {exprs['due']} AS due_value,
            {exprs['progress']} AS progress_value,
            COUNT(*) OVER() AS total_count
        FROM table_records r
        WHERE r.table_id = :table_id
          AND ({visibility})
          AND ({assigned})
          AND ({incomplete})
          AND ({due_condition})
        ORDER BY {', '.join(order_parts)}
        LIMIT :fetch_limit
        """
    )
    rows = list((await db.execute(statement, params)).mappings().all())
    total = int(rows[0]["total_count"]) if rows else 0
    return rows, total


async def _due_section(
    db: AsyncSession,
    *,
    contexts: Sequence[TaskTableContext],
    user_id: int,
    limit: int,
    offset: int,
    tz: ZoneInfo,
) -> Dict[str, Any]:
    """Return exact rolling due buckets without materializing whole projects."""
    now_local = _utcnow().astimezone(tz)
    fetch_limit = offset + limit + 1
    result: Dict[str, Any] = {}
    for bucket in ("overdue", "today", "next24h", "next3d", "next7d"):
        bucket_items: List[Dict[str, Any]] = []
        total = 0
        for ctx in contexts:
            if (
                not ctx.config.get("assigneeFieldId")
                or not ctx.config.get("dueDateFieldId")
            ):
                continue
            rows, table_total = await _fetch_due_bucket_rows(
                db,
                ctx=ctx,
                user_id=user_id,
                bucket=bucket,
                fetch_limit=fetch_limit,
                now_local=now_local,
            )
            total += table_total
            bucket_items.extend(
                _row_to_task(ctx, row, tz=tz, now_local=now_local)
                for row in rows
            )
        bucket_items.sort(key=_task_sort_key)
        page = bucket_items[offset : offset + limit]
        for item in page:
            item.pop("_dueSort", None)
        result[bucket] = {
            "items": page,
            "totalCount": total,
            "pageInfo": _page_info(
                offset,
                len(page),
                offset + len(page) < total,
            ),
        }
    return result

def _overdue_condition(
    ctx: TaskTableContext,
    dialect: str,
    params: Dict[str, Any],
    *,
    now_local: datetime,
    alias: str = "r",
) -> Optional[str]:
    due_field = ctx.config.get("dueDateFieldId")
    if not due_field:
        return None
    params["overdue_due_key"] = str(due_field)
    params["overdue_due_path"] = _json_path(str(due_field))
    due_expr = _json_text_expr(
        dialect,
        alias,
        "overdue_due_key",
        "overdue_due_path",
    )
    return _due_bucket_condition(
        dialect=dialect,
        due_expr=due_expr,
        bucket="overdue",
        params=params,
        now_local=now_local,
    )


async def _project_aggregate(
    db: AsyncSession,
    *,
    ctx: TaskTableContext,
    user_id: int,
    tz: ZoneInfo,
) -> Dict[str, Any]:
    dialect = _dialect_name(db)
    params: Dict[str, Any] = {
        "table_id": ctx.table_id,
        "user_id": int(user_id),
    }
    visibility = _visibility_sql(ctx, dialect, params)
    completed_sql, _, blocked_sql = _completion_sql(ctx, dialect, params)
    overdue_sql = _overdue_condition(
        ctx,
        dialect,
        params,
        now_local=_utcnow().astimezone(tz),
    )
    progress_field = ctx.config.get("progressFieldId")
    if progress_field:
        params["project_progress_key"] = str(progress_field)
        params["project_progress_path"] = _json_path(str(progress_field))
        progress_expr = _json_text_expr(
            dialect,
            "r",
            "project_progress_key",
            "project_progress_path",
        )
        avg_expr = (
            f"AVG(NULLIF({progress_expr}, '')::double precision)"
            if dialect == "postgresql"
            else f"AVG(CAST(NULLIF({progress_expr}, '') AS REAL))"
        )
    else:
        avg_expr = "NULL"

    completed_case = (
        f"CASE WHEN {completed_sql} THEN 1 ELSE 0 END"
        if completed_sql
        else "0"
    )
    blocked_case = (
        f"CASE WHEN {blocked_sql} THEN 1 ELSE 0 END"
        if blocked_sql
        else "0"
    )
    incomplete_guard = _not_true(completed_sql) if completed_sql else "1=1"
    overdue_case = (
        f"CASE WHEN ({incomplete_guard}) AND ({overdue_sql}) THEN 1 ELSE 0 END"
        if overdue_sql
        else "0"
    )
    statement = text(
        f"""
        SELECT
            COUNT(*) AS total_count,
            SUM({completed_case}) AS completed_count,
            SUM({blocked_case}) AS blocked_count,
            SUM({overdue_case}) AS overdue_count,
            {avg_expr} AS avg_progress
        FROM table_records r
        WHERE r.table_id = :table_id AND ({visibility})
        """
    )
    row = (await db.execute(statement, params)).mappings().one()
    total = int(row.get("total_count") or 0)
    completed = int(row.get("completed_count") or 0)
    avg_progress = row.get("avg_progress")
    progress = (
        round((completed / total) * 100, 2)
        if avg_progress is None and total
        else round(float(avg_progress or 0), 2)
    )
    return {
        "tableId": ctx.table_id,
        "tableName": ctx.table.name,
        "workspaceId": ctx.table.workspace_id,
        "totalTasks": total,
        "completedTasks": completed,
        "incompleteTasks": max(0, total - completed),
        "progress": progress,
        "overdueCount": int(row.get("overdue_count") or 0),
        "blockedCount": int(row.get("blocked_count") or 0),
        "completionKnown": bool(
            ctx.config.get("statusFieldId")
            and ctx.config.get("completedStatusValues")
        ),
        "deepLink": _table_deep_link(ctx.table),
    }


async def _projects_section(
    db: AsyncSession,
    *,
    contexts: Sequence[TaskTableContext],
    user_id: int,
    limit: int,
    offset: int,
    tz: ZoneInfo,
) -> Dict[str, Any]:
    projects = [
        await _project_aggregate(
            db,
            ctx=ctx,
            user_id=user_id,
            tz=tz,
        )
        for ctx in contexts
    ]
    projects.sort(
        key=lambda item: (
            -(int(item["overdueCount"]) + int(item["blockedCount"])),
            float(item["progress"]),
            str(item["tableName"] or ""),
            str(item["tableId"]),
        )
    )
    page = projects[offset : offset + limit]
    return {
        "items": page,
        "totalCount": len(projects),
        "pageInfo": _page_info(
            offset,
            len(page),
            offset + len(page) < len(projects),
        ),
    }


def _recent_target_key(
    entity_type: str,
    entity_id: str,
    table_id: Optional[str],
) -> str:
    normalized_type = str(entity_type or "").strip().lower()
    normalized_entity = str(entity_id or "").strip()
    normalized_table = str(table_id or "").strip()
    if normalized_type == "table" and not normalized_table:
        normalized_table = normalized_entity
    return f"{normalized_type}:{normalized_table}:{normalized_entity}"


def _parse_visited_at(value: Any) -> datetime:
    now = _utcnow()
    if value is None:
        return now
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return min(parsed.astimezone(timezone.utc), now + timedelta(minutes=5))
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except Exception:
            return now
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return now
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    else:
        return now
    return now if parsed > now + timedelta(minutes=5) else parsed


async def _canonical_table_view_id(
    db: AsyncSession,
    *,
    table: WorkspaceItem,
    requested_view_id: Optional[str],
) -> str:
    """Resolve a recent target view against the table's current views."""
    candidates: List[str] = []
    if requested_view_id:
        candidates.append(str(requested_view_id))
    if table.default_view_id:
        candidates.append(str(table.default_view_id))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        result = await db.execute(
            select(TableView.id).where(
                TableView.table_id == str(table.id),
                TableView.id == candidate,
            )
        )
        if result.scalars().first() is not None:
            return candidate

    fallback = await db.execute(
        select(TableView.id)
        .where(TableView.table_id == str(table.id))
        .order_by(TableView.id.asc())
        .limit(1)
    )
    return str(fallback.scalars().first() or "v1")


async def _canonical_recent_target(
    db: AsyncSession,
    *,
    user_id: int,
    raw_target: Mapping[str, Any],
) -> Dict[str, Any]:
    if not isinstance(raw_target, Mapping):
        raise MyWorkValidationError("Recent target must be an object")
    entity_type = str(raw_target.get("entityType") or "").strip().lower()
    if entity_type not in {"table", "dashboard", "record"}:
        raise MyWorkValidationError("Unsupported recent target type")
    entity_id = str(raw_target.get("entityId") or "").strip()
    if not entity_id:
        raise MyWorkValidationError("Recent target requires entityId")

    nested_table = raw_target.get("table")
    nested_table = nested_table if isinstance(nested_table, Mapping) else {}
    table_id = str(
        raw_target.get("tableId")
        or nested_table.get("id")
        or (entity_id if entity_type == "table" else "")
    ).strip() or None
    view_id = str(
        raw_target.get("viewId")
        or nested_table.get("defaultViewId")
        or ""
    ).strip() or None

    if entity_type in {"table", "dashboard"}:
        result = await db.execute(
            select(WorkspaceItem).where(
                WorkspaceItem.id == entity_id,
                WorkspaceItem.type == entity_type,
            )
        )
        item = result.scalars().first()
        if item is None:
            raise MyWorkValidationError("Recent target no longer exists")
        permission = await get_effective_permission_for_item(
            db,
            user_id,
            item.id,
        )
        if not permission_allows(permission, "read"):
            raise MyWorkValidationError("Recent target is not accessible")
        if entity_type == "table":
            canonical_view_id = await _canonical_table_view_id(
                db,
                table=item,
                requested_view_id=view_id,
            )
            return {
                "targetKey": _recent_target_key("table", item.id, item.id),
                "entityType": "table",
                "entityId": item.id,
                "workspaceId": item.workspace_id,
                "tableId": item.id,
                "viewId": canonical_view_id,
                "title": item.name,
                "subtitle": "数据表",
                "deepLink": _table_deep_link(item, canonical_view_id),
                "visitedAt": _parse_visited_at(raw_target.get("visitedAt")),
            }
        return {
            "targetKey": _recent_target_key("dashboard", item.id, None),
            "entityType": "dashboard",
            "entityId": item.id,
            "workspaceId": item.workspace_id,
            "tableId": None,
            "viewId": None,
            "title": item.name,
            "subtitle": "仪表盘",
            "deepLink": f"/workbench/{quote(str(item.id), safe='')}",
            "visitedAt": _parse_visited_at(raw_target.get("visitedAt")),
        }

    if not table_id:
        raise MyWorkValidationError("Record recent target requires tableId")
    table_result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == table_id,
            WorkspaceItem.type == "table",
        )
    )
    table = table_result.scalars().first()
    if table is None:
        raise MyWorkValidationError("Record table no longer exists")
    permission = await get_effective_permission_for_item(db, user_id, table_id)
    if not permission_allows(permission, "read"):
        raise MyWorkValidationError("Record recent target is not accessible")
    try:
        record = await require_record_access(
            db,
            table_id,
            entity_id,
            user_id=user_id,
            table_permission=str(permission),
        )
    except PermissionError as exc:
        raise MyWorkValidationError(
            "Record recent target is not accessible"
        ) from exc

    title = entity_id
    profile_result = await db.execute(
        select(TableTaskProfile).where(
            TableTaskProfile.table_id == table_id
        )
    )
    profile = profile_result.scalars().first()
    if profile and isinstance(profile.config, Mapping):
        title_field = profile.config.get("titleFieldId")
        if title_field and isinstance(record.data, Mapping):
            title = str(record.data.get(str(title_field)) or entity_id)
    target_view = await _canonical_table_view_id(
        db,
        table=table,
        requested_view_id=view_id,
    )
    return {
        "targetKey": _recent_target_key("record", entity_id, table_id),
        "entityType": "record",
        "entityId": entity_id,
        "workspaceId": table.workspace_id,
        "tableId": table_id,
        "viewId": target_view,
        "title": title,
        "subtitle": table.name,
        "deepLink": (
            f"/workbench/{quote(table_id, safe='')}/"
            f"{quote(str(target_view), safe='')}"
            f"?recordId={quote(entity_id, safe='')}"
        ),
        "visitedAt": _parse_visited_at(raw_target.get("visitedAt")),
    }


async def _persist_canonical_recent_target(
    db: AsyncSession,
    *,
    user_id: int,
    canonical: Mapping[str, Any],
) -> MyWorkRecentTarget:
    result = await db.execute(
        select(MyWorkRecentTarget).where(
            MyWorkRecentTarget.user_id == user_id,
            MyWorkRecentTarget.target_key == str(canonical["targetKey"]),
        )
    )
    target = result.scalars().first()
    if target is None:
        target = MyWorkRecentTarget(
            id=f"mwr_{uuid.uuid4().hex}",
            user_id=user_id,
            target_key=str(canonical["targetKey"]),
            entity_type=str(canonical["entityType"]),
            entity_id=str(canonical["entityId"]),
            workspace_id=canonical.get("workspaceId"),
            table_id=canonical.get("tableId"),
            view_id=canonical.get("viewId"),
            visited_at=canonical["visitedAt"],
        )
        db.add(target)
    else:
        target.workspace_id = canonical.get("workspaceId")
        target.table_id = canonical.get("tableId")
        target.view_id = canonical.get("viewId")
        target.entity_type = str(canonical["entityType"])
        target.entity_id = str(canonical["entityId"])
        existing_visited = target.visited_at
        if existing_visited.tzinfo is None:
            existing_visited = existing_visited.replace(tzinfo=timezone.utc)
        if canonical["visitedAt"] >= existing_visited:
            target.visited_at = canonical["visitedAt"]
    await db.flush()
    return target


async def _prune_recent_targets(
    db: AsyncSession,
    *,
    user_id: int,
) -> None:
    result = await db.execute(
        select(MyWorkRecentTarget.id)
        .where(MyWorkRecentTarget.user_id == user_id)
        .order_by(
            MyWorkRecentTarget.visited_at.desc(),
            MyWorkRecentTarget.id.desc(),
        )
        .offset(RECENT_RETENTION)
    )
    stale_ids = list(result.scalars().all())
    if stale_ids:
        await db.execute(
            delete(MyWorkRecentTarget).where(
                MyWorkRecentTarget.id.in_(stale_ids)
            )
        )


async def upsert_recent_target(
    db: AsyncSession,
    *,
    user_id: int,
    raw_target: Mapping[str, Any],
) -> Dict[str, Any]:
    canonical = await _canonical_recent_target(
        db,
        user_id=user_id,
        raw_target=raw_target,
    )
    await _persist_canonical_recent_target(
        db,
        user_id=user_id,
        canonical=canonical,
    )
    await _prune_recent_targets(db, user_id=user_id)
    await db.commit()
    payload = dict(canonical)
    payload["visitedAt"] = canonical["visitedAt"].isoformat()
    payload.pop("targetKey", None)
    return payload


async def import_recent_targets(
    db: AsyncSession,
    *,
    user_id: int,
    raw_targets: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(raw_targets, Sequence) or isinstance(
        raw_targets,
        (str, bytes),
    ):
        raise MyWorkValidationError("targets must be a list")
    imported = 0
    skipped: List[Dict[str, Any]] = []
    for index, raw in enumerate(list(raw_targets)[:50]):
        try:
            canonical = await _canonical_recent_target(
                db,
                user_id=user_id,
                raw_target=raw,
            )
            await _persist_canonical_recent_target(
                db,
                user_id=user_id,
                canonical=canonical,
            )
            imported += 1
        except MyWorkValidationError as exc:
            skipped.append({"index": index, "reason": str(exc)})
    await _prune_recent_targets(db, user_id=user_id)
    await db.commit()
    return {
        "importedCount": imported,
        "skippedCount": len(skipped),
        "skipped": skipped,
    }


async def remove_recent_target(
    db: AsyncSession,
    *,
    user_id: int,
    entity_type: str,
    entity_id: str,
    table_id: Optional[str] = None,
) -> bool:
    key = _recent_target_key(
        str(entity_type or "").strip().lower(),
        str(entity_id or "").strip(),
        str(table_id).strip() if table_id else None,
    )
    result = await db.execute(
        select(MyWorkRecentTarget).where(
            MyWorkRecentTarget.user_id == user_id,
            MyWorkRecentTarget.target_key == key,
        )
    )
    target = result.scalars().first()
    if target is None:
        return False
    await db.delete(target)
    await db.commit()
    return True


async def _recent_section(
    db: AsyncSession,
    *,
    user_id: int,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    result = await db.execute(
        select(MyWorkRecentTarget)
        .where(MyWorkRecentTarget.user_id == user_id)
        .order_by(
            MyWorkRecentTarget.visited_at.desc(),
            MyWorkRecentTarget.id.desc(),
        )
        .limit(RECENT_RETENTION)
    )
    visible: List[Dict[str, Any]] = []
    for target in result.scalars().all():
        raw = {
            "entityType": target.entity_type,
            "entityId": target.entity_id,
            "tableId": target.table_id,
            "viewId": target.view_id,
            "visitedAt": target.visited_at,
        }
        try:
            canonical = await _canonical_recent_target(
                db,
                user_id=user_id,
                raw_target=raw,
            )
        except MyWorkValidationError:
            # Permission loss/deletion is reflected immediately.
            continue
        item = dict(canonical)
        item["visitedAt"] = canonical["visitedAt"].isoformat()
        item.pop("targetKey", None)
        visible.append(item)
    page = visible[offset : offset + limit]
    return {
        "items": page,
        "totalCount": len(visible),
        "pageInfo": _page_info(
            offset,
            len(page),
            offset + len(page) < len(visible),
        ),
    }


async def _load_current_records_for_activity(
    db: AsyncSession,
    *,
    candidates: Sequence[tuple[ChangeItem, ChangeSet]],
) -> Dict[tuple[str, str], TableRecord]:
    ids_by_table: Dict[str, set[str]] = {}
    for item, _ in candidates:
        if item.entity_type == "record":
            ids_by_table.setdefault(str(item.table_id), set()).add(
                str(item.entity_id)
            )
            continue
        if item.entity_type == "comment":
            payload = (
                item.after_data
                if isinstance(item.after_data, Mapping)
                else item.before_data
                if isinstance(item.before_data, Mapping)
                else {}
            )
            parent_record_id = payload.get("recordId")
            if parent_record_id:
                ids_by_table.setdefault(str(item.table_id), set()).add(
                    str(parent_record_id)
                )
    output: Dict[tuple[str, str], TableRecord] = {}
    for table_id, record_ids in ids_by_table.items():
        result = await db.execute(
            select(TableRecord).where(
                TableRecord.table_id == table_id,
                TableRecord.id.in_(list(record_ids)),
            )
        )
        for record in result.scalars().all():
            output[(str(record.table_id), str(record.id))] = record
    return output


def _activity_kinds(
    ctx: TaskTableContext,
    item: ChangeItem,
) -> List[str]:
    if item.entity_type == "comment":
        return ["comment.created"]
    if item.entity_type != "record":
        return [f"{item.entity_type}.changed"]
    if item.before_data is None and item.after_data is not None:
        return ["record.created"]
    if item.before_data is not None and item.after_data is None:
        return ["record.deleted"]
    changed = {str(value) for value in item.changed_fields or []}
    kinds: List[str] = []
    status = ctx.config.get("statusFieldId")
    assignee = ctx.config.get("assigneeFieldId")
    if status and str(status) in changed:
        kinds.append("task.status_changed")
    if assignee and str(assignee) in changed:
        kinds.append("task.assignee_changed")
    return kinds or ["record.updated"]


async def _activity_section(
    db: AsyncSession,
    *,
    contexts: Sequence[TaskTableContext],
    user_id: int,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    """Return a raw-history cursor feed while filtering by current visibility.

    The activity cursor advances over raw ChangeItem positions rather than the
    number of visible rows. This matters when a user loses access to many
    records: an empty filtered page must still make forward progress instead of
    returning the same cursor forever.
    """
    by_table = {ctx.table_id: ctx for ctx in contexts}
    if not by_table:
        return {
            "items": [],
            "pageInfo": {
                "offset": offset,
                "hasMore": False,
                "nextCursor": None,
                "scannedCount": 0,
            },
        }

    batch_size = min(500, max(100, limit * 10))
    raw_offset = offset
    scanned = 0
    visible: List[Dict[str, Any]] = []
    has_more = False

    while len(visible) < limit and scanned < ACTIVITY_SCAN_CAP:
        result = await db.execute(
            select(ChangeItem, ChangeSet)
            .join(ChangeSet, ChangeSet.id == ChangeItem.change_set_id)
            .where(
                ChangeItem.table_id.in_(list(by_table.keys())),
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
        candidates = list(result.all())
        if not candidates:
            has_more = False
            break

        records = await _load_current_records_for_activity(
            db,
            candidates=candidates,
        )
        actor_ids = {
            int(change_set.actor_id)
            for _, change_set in candidates
            if change_set.actor_id is not None
        }
        actors: Dict[int, User] = {}
        if actor_ids:
            actor_result = await db.execute(
                select(User).where(User.id.in_(actor_ids))
            )
            actors = {
                int(user.id): user
                for user in actor_result.scalars().all()
            }

        processed = 0
        for item, change_set in candidates:
            processed += 1
            raw_offset += 1
            scanned += 1
            ctx = by_table.get(str(item.table_id))
            if ctx is None:
                continue

            if item.entity_type == "record":
                parent_record_id = str(item.entity_id)
            elif item.entity_type == "comment":
                payload = (
                    item.after_data
                    if isinstance(item.after_data, Mapping)
                    else item.before_data
                    if isinstance(item.before_data, Mapping)
                    else {}
                )
                parent_record_id = str(payload.get("recordId") or "")
                if not parent_record_id:
                    continue
            else:
                continue

            current = records.get((ctx.table_id, parent_record_id))
            if current is None:
                # Deleted/stale records fail closed to avoid history leakage.
                continue
            if not record_is_visible(
                policy=ctx.row_policy,
                user_id=user_id,
                table_permission=ctx.permission,
                created_by_user_id=current.created_by_user_id,
                data=dict(current.data or {}),
            ):
                continue

            title_field = ctx.config.get("titleFieldId")
            title = (
                str((current.data or {}).get(str(title_field)) or parent_record_id)
                if title_field
                else parent_record_id
            )
            actor = (
                actors.get(int(change_set.actor_id))
                if change_set.actor_id is not None
                else None
            )
            visible.append(
                {
                    "changeSetId": change_set.id,
                    "time": (
                        change_set.created_at.isoformat()
                        if change_set.created_at
                        else None
                    ),
                    "actor": {
                        "id": change_set.actor_id,
                        "type": change_set.actor_type,
                        "name": (
                            actor.name
                            if actor and actor.name
                            else actor.email
                            if actor and actor.email
                            else None
                        ),
                    },
                    "entity": {
                        "type": item.entity_type,
                        "id": item.entity_id,
                        "title": title,
                        "tableId": ctx.table_id,
                        "tableName": ctx.table.name,
                        "workspaceId": ctx.table.workspace_id,
                    },
                    "kinds": _activity_kinds(ctx, item),
                    "operation": change_set.operation,
                    "summary": change_set.summary,
                    "deepLink": _record_deep_link(
                        ctx,
                        parent_record_id,
                    ),
                }
            )
            if len(visible) >= limit or scanned >= ACTIVITY_SCAN_CAP:
                break

        if processed < len(candidates):
            has_more = True
            break
        if len(candidates) < batch_size:
            has_more = False
            break
        # A full raw batch means more history may exist. Continue scanning if
        # this page still needs visible items; otherwise return the next raw
        # cursor immediately.
        has_more = True

    return {
        "items": visible[:limit],
        "pageInfo": {
            "offset": offset,
            "hasMore": bool(has_more),
            "nextCursor": _encode_cursor(raw_offset) if has_more else None,
            "scannedCount": scanned,
        },
    }


async def _weekly_completed_count(
    db: AsyncSession,
    *,
    contexts: Sequence[TaskTableContext],
    user_id: int,
    tz: ZoneInfo,
) -> int:
    now_local = _utcnow().astimezone(tz)
    week_start_local = (
        now_local - timedelta(days=now_local.weekday())
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_utc = week_start_local.astimezone(timezone.utc)
    total = 0

    for ctx in contexts:
        status_field = ctx.config.get("statusFieldId")
        completed = [
            str(value)
            for value in ctx.config.get("completedStatusValues") or []
        ]
        if (
            not status_field
            or not completed
            or not ctx.config.get("assigneeFieldId")
        ):
            continue

        dialect = _dialect_name(db)
        params: Dict[str, Any] = {
            "table_id": ctx.table_id,
            "user_id": int(user_id),
            "week_start": week_start_utc,
            "history_status_key": str(status_field),
            "history_status_path": _json_path(str(status_field)),
            "changed_field": str(status_field),
            "changed_field_array": json.dumps(
                [str(status_field)],
                separators=(",", ":"),
            ),
        }
        visibility = _visibility_sql(
            ctx,
            dialect,
            params,
            alias="r",
        )
        assignee_field = str(ctx.config["assigneeFieldId"])
        params["history_assignee_key"] = assignee_field
        params["history_assignee_path"] = _json_path(assignee_field)
        params["history_assignee_user"] = str(user_id)
        params["history_assignee_array"] = json.dumps(
            [str(user_id)],
            separators=(",", ":"),
        )
        assigned_at_completion = _member_match_sql_for_column(
            dialect,
            column_sql="ci.after_data",
            field_key_param="history_assignee_key",
            field_path_param="history_assignee_path",
            user_param="history_assignee_user",
            array_param="history_assignee_array",
        )

        history_status = _json_text_expr_for_column(
            dialect,
            "ci.after_data",
            "history_status_key",
            "history_status_path",
        )
        completed_sql = _sql_in(
            history_status,
            completed,
            params,
            "history_completed",
        )
        changed_sql = (
            "CAST(ci.changed_fields AS jsonb) "
            "@> CAST(:changed_field_array AS jsonb)"
            if dialect == "postgresql"
            else (
                "EXISTS (SELECT 1 FROM json_each(ci.changed_fields) AS changed "
                "WHERE CAST(changed.value AS TEXT) = :changed_field)"
            )
        )
        statement = text(
            f"""
            SELECT COUNT(DISTINCT ci.entity_id) AS completed_count
            FROM change_items ci
            JOIN change_sets cs ON cs.id = ci.change_set_id
            JOIN table_records r
              ON r.table_id = ci.table_id AND r.id = ci.entity_id
            WHERE ci.table_id = :table_id
              AND ci.entity_type = 'record'
              AND cs.created_at >= :week_start
              AND cs.status = 'applied'
              AND ({visibility})
              AND ({assigned_at_completion})
              AND ({changed_sql})
              AND ({completed_sql})
            """
        )
        row = (await db.execute(statement, params)).mappings().one()
        total += int(row.get("completed_count") or 0)
    return total


async def _personal_kpi_for_context(
    db: AsyncSession,
    *,
    ctx: TaskTableContext,
    user_id: int,
    tz: ZoneInfo,
) -> Dict[str, int]:
    if not ctx.config.get("assigneeFieldId"):
        return {"incomplete": 0, "risk": 0}
    dialect = _dialect_name(db)
    params: Dict[str, Any] = {
        "table_id": ctx.table_id,
        "user_id": int(user_id),
    }
    visibility = _visibility_sql(ctx, dialect, params)
    assigned = _assigned_sql(ctx, dialect, params)
    if not assigned:
        return {"incomplete": 0, "risk": 0}
    completed_sql, _, blocked_sql = _completion_sql(
        ctx,
        dialect,
        params,
    )
    incomplete = _not_true(completed_sql) if completed_sql else "1=1"
    overdue = _overdue_condition(
        ctx,
        dialect,
        params,
        now_local=_utcnow().astimezone(tz),
    )
    risks = [part for part in (overdue, blocked_sql) if part]
    risk_sql = (
        " OR ".join(f"({part})" for part in risks)
        if risks
        else "0=1"
    )
    statement = text(
        f"""
        SELECT
            SUM(
                CASE WHEN ({incomplete}) THEN 1 ELSE 0 END
            ) AS incomplete_count,
            SUM(
                CASE WHEN ({incomplete}) AND ({risk_sql})
                THEN 1 ELSE 0 END
            ) AS risk_count
        FROM table_records r
        WHERE r.table_id = :table_id
          AND ({visibility})
          AND ({assigned})
        """
    )
    row = (await db.execute(statement, params)).mappings().one()
    return {
        "incomplete": int(row.get("incomplete_count") or 0),
        "risk": int(row.get("risk_count") or 0),
    }


async def _kpi_section(
    db: AsyncSession,
    *,
    contexts: Sequence[TaskTableContext],
    user_id: int,
    tz: ZoneInfo,
) -> Dict[str, Any]:
    my_incomplete = 0
    risk = 0
    active_projects = 0
    for ctx in contexts:
        personal = await _personal_kpi_for_context(
            db,
            ctx=ctx,
            user_id=user_id,
            tz=tz,
        )
        my_incomplete += personal["incomplete"]
        risk += personal["risk"]
        project = await _project_aggregate(
            db,
            ctx=ctx,
            user_id=user_id,
            tz=tz,
        )
        if project["incompleteTasks"] > 0:
            active_projects += 1
    weekly_completed = await _weekly_completed_count(
        db,
        contexts=contexts,
        user_id=user_id,
        tz=tz,
    )
    return {
        "myIncompleteCount": my_incomplete,
        "completedThisWeekCount": weekly_completed,
        "activeProjectCount": active_projects,
        "overdueOrRiskCount": risk,
    }


async def build_my_work(
    db: AsyncSession,
    *,
    user_id: int,
    sections: Optional[Sequence[str]] = None,
    limit: int = 20,
    cursors: Optional[Mapping[str, Any]] = None,
    timezone_name: str = "UTC",
    task_state: str = "all",
) -> Dict[str, Any]:
    safe_limit = _safe_limit(limit)
    tz = _timezone(timezone_name)
    normalized_state = str(task_state or "all").strip().lower()
    if normalized_state not in ALLOWED_TASK_STATES:
        raise MyWorkValidationError(
            "taskState must be one of: all, in_progress, not_started, completed"
        )

    requested = list(sections or DEFAULT_SECTIONS)
    unknown = [
        section
        for section in requested
        if section not in ALLOWED_SECTIONS
    ]
    if unknown:
        raise MyWorkValidationError(
            f"Unknown My Work section: {unknown[0]}"
        )
    requested = list(dict.fromkeys(requested))

    started = time.perf_counter()
    contexts: Optional[List[TaskTableContext]] = None
    activity_context_cache: Optional[List[TaskTableContext]] = None

    async def task_contexts() -> List[TaskTableContext]:
        nonlocal contexts
        if contexts is None:
            contexts = await _task_table_contexts(
                db,
                user_id=user_id,
            )
        return contexts

    async def activity_contexts() -> List[TaskTableContext]:
        nonlocal activity_context_cache
        if activity_context_cache is None:
            activity_context_cache = await _activity_table_contexts(
                db,
                user_id=user_id,
            )
        return activity_context_cache

    payload: Dict[str, Any] = {
        "generatedAt": _utcnow().isoformat(),
        "timezone": timezone_name or "UTC",
        "taskState": normalized_state,
        "sections": {},
        "cache": {
            "mode": "none",
            "reason": (
                "Permission-sensitive v1 payloads are intentionally not cached "
                "so table/row permission loss is reflected immediately."
            ),
        },
        "performance": {"sectionMs": {}},
    }

    for section in requested:
        section_started = time.perf_counter()
        try:
            offset = _cursor_for(cursors, section)
            # PostgreSQL marks the current transaction aborted after a SQL
            # statement error. A savepoint per section is therefore required
            # for the public "one section may fail without blanking My Work"
            # contract to be true on the production database, not just SQLite.
            async with db.begin_nested():
                if section == "tasks":
                    result = await _tasks_section(
                        db,
                        contexts=await task_contexts(),
                        user_id=user_id,
                        task_state=normalized_state,
                        limit=safe_limit,
                        offset=offset,
                        tz=tz,
                    )
                elif section == "due":
                    result = await _due_section(
                        db,
                        contexts=await task_contexts(),
                        user_id=user_id,
                        limit=safe_limit,
                        offset=offset,
                        tz=tz,
                    )
                elif section == "projects":
                    result = await _projects_section(
                        db,
                        contexts=await task_contexts(),
                        user_id=user_id,
                        limit=safe_limit,
                        offset=offset,
                        tz=tz,
                    )
                elif section == "recent":
                    result = await _recent_section(
                        db,
                        user_id=user_id,
                        limit=safe_limit,
                        offset=offset,
                    )
                elif section == "activity":
                    result = await _activity_section(
                        db,
                        contexts=await activity_contexts(),
                        user_id=user_id,
                        limit=safe_limit,
                        offset=offset,
                    )
                else:
                    result = await _kpi_section(
                        db,
                        contexts=await task_contexts(),
                        user_id=user_id,
                        tz=tz,
                    )
            payload["sections"][section] = {
                "status": "ok",
                "data": result,
            }
        except MyWorkValidationError:
            raise
        except Exception:
            logger.exception(
                "My Work section failed: %s",
                section,
            )
            payload["sections"][section] = {
                "status": "error",
                "error": {
                    "code": "SECTION_FAILED",
                    "message": f"{section} is temporarily unavailable",
                },
            }
        payload["performance"]["sectionMs"][section] = round(
            (time.perf_counter() - section_started) * 1000,
            2,
        )

    payload["performance"]["totalMs"] = round(
        (time.perf_counter() - started) * 1000,
        2,
    )
    return payload
