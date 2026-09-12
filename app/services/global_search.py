from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence

from sqlalchemy import String, and_, case, cast, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    WorkspaceItem,
    WorkspaceItemPermission,
)
from app.models.workspace_member import Workspace, WorkspaceMember
from app.services.row_permissions import record_is_visible
from app.services.workspace import normalize_permission, permission_allows, role_permission


DEFAULT_GLOBAL_SEARCH_LIMIT = 20
MAX_GLOBAL_SEARCH_LIMIT = 50
MAX_GLOBAL_SEARCH_QUERY_LENGTH = 200
RECORD_CANDIDATE_CHUNK = 200
MAX_RECORD_CANDIDATES_SCANNED = 5000

TEXT_SEARCH_FIELD_TYPES = {"text", "url", "email", "phone"}
SUPPORTED_ENTITY_TYPES = {"workspace", "folder", "table", "dashboard", "record"}
ENTITY_TYPE_ALIASES = {"task": "record"}


class QNoteSearchAdapter(Protocol):
    """Future cross-product search boundary.

    QTable must never read QNote's database directly. A QNote integration can
    implement this contract and return already permission-filtered results using
    the current user's bearer token.
    """

    async def search(
        self,
        *,
        keyword: str,
        workspace_id: Optional[str],
        cursor: Optional[str],
        limit: int,
        bearer_token: str,
    ) -> Dict[str, Any]:
        ...


@dataclass(frozen=True)
class AccessibleSearchContext:
    workspaces: Dict[str, str]
    items: Dict[str, WorkspaceItem]
    permissions: Dict[str, str]


def _normalize_keyword(value: str) -> str:
    keyword = str(value or "").strip()
    if not keyword:
        raise ValueError("Search keyword is required")
    if len(keyword) > MAX_GLOBAL_SEARCH_QUERY_LENGTH:
        raise ValueError(
            f"Search keyword must be <= {MAX_GLOBAL_SEARCH_QUERY_LENGTH} characters"
        )
    return keyword


def _normalize_entity_types(entity_types: Optional[Sequence[str]]) -> List[str]:
    if not entity_types:
        return sorted(SUPPORTED_ENTITY_TYPES)

    normalized: List[str] = []
    for raw in entity_types:
        value = str(raw or "").strip()
        value = ENTITY_TYPE_ALIASES.get(value, value)
        if value not in SUPPORTED_ENTITY_TYPES:
            raise ValueError(f"Unsupported search entity type: {raw}")
        if value not in normalized:
            normalized.append(value)
    return normalized


def _cursor_fingerprint(
    *,
    keyword: str,
    workspace_id: Optional[str],
    entity_types: Sequence[str],
) -> str:
    payload = json.dumps(
        {
            "keyword": keyword.casefold(),
            "workspaceId": workspace_id or "",
            "entityTypes": sorted(entity_types),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(offset: int, fingerprint: str) -> str:
    raw = json.dumps(
        {"offset": max(0, int(offset)), "fingerprint": fingerprint},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: Optional[str], fingerprint: str) -> int:
    if not cursor:
        return 0
    value = str(cursor).strip()
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("fingerprint") != fingerprint:
            raise ValueError("Search cursor does not match the current query")
        return max(0, int(payload.get("offset") or 0))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Invalid search cursor") from exc


def _score_name(name: str, keyword: str) -> Optional[int]:
    value = str(name or "").strip()
    if not value:
        return None
    folded = value.casefold()
    query = keyword.casefold()
    if folded == query:
        return 100
    if folded.startswith(query):
        return 85
    if query in folded:
        return 65
    return None


def _snippet(value: str, keyword: str, max_length: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_length:
        return text
    lower = text.casefold()
    index = lower.find(keyword.casefold())
    if index < 0:
        return text[: max_length - 1] + "…"
    start = max(0, index - max_length // 3)
    end = min(len(text), start + max_length)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(
            part for part in (_display_value(item) for item in value) if part
        )
    if isinstance(value, dict):
        for key in ("label", "name", "title", "value", "id"):
            if key in value:
                rendered = _display_value(value.get(key))
                if rendered:
                    return rendered
        return ""
    return str(value)


async def _load_accessible_context(
    db: AsyncSession,
    *,
    user_id: int,
    workspace_id: Optional[str],
) -> AccessibleSearchContext:
    membership_stmt = (
        select(WorkspaceMember, Workspace)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user_id)
    )
    if workspace_id:
        membership_stmt = membership_stmt.where(
            WorkspaceMember.workspace_id == workspace_id
        )

    membership_result = await db.execute(membership_stmt)
    memberships = list(membership_result.all())
    if not memberships:
        return AccessibleSearchContext(workspaces={}, items={}, permissions={})

    workspace_names = {
        member.workspace_id: (workspace.name or member.workspace_id)
        for member, workspace in memberships
    }
    workspace_ids = list(workspace_names)

    item_result = await db.execute(
        select(WorkspaceItem).where(WorkspaceItem.workspace_id.in_(workspace_ids))
    )
    items = list(item_result.scalars().all())
    item_by_id = {item.id: item for item in items}
    if not items:
        return AccessibleSearchContext(
            workspaces=workspace_names,
            items={},
            permissions={},
        )

    override_result = await db.execute(
        select(WorkspaceItemPermission).where(
            WorkspaceItemPermission.user_id == user_id,
            WorkspaceItemPermission.item_id.in_(list(item_by_id)),
        )
    )
    overrides = {
        row.item_id: normalize_permission(row.permission)
        for row in override_result.scalars().all()
    }
    role_by_workspace = {
        member.workspace_id: role_permission(member.role)
        for member, _workspace in memberships
    }

    permission_cache: Dict[str, str] = {}

    def resolve(item_id: str, trail: Optional[set[str]] = None) -> str:
        if item_id in permission_cache:
            return permission_cache[item_id]
        item = item_by_id[item_id]
        if item_id in overrides:
            permission_cache[item_id] = overrides[item_id]
            return permission_cache[item_id]

        seen = set(trail or set())
        if item_id in seen:
            # Corrupted parent cycles fail closed to the workspace role. This
            # keeps search from recursing forever while matching the ordinary
            # permission fallback semantics.
            permission_cache[item_id] = role_by_workspace.get(
                item.workspace_id, "read"
            )
            return permission_cache[item_id]
        seen.add(item_id)

        if item.parent_id and item.parent_id in item_by_id:
            permission_cache[item_id] = resolve(item.parent_id, seen)
        else:
            permission_cache[item_id] = role_by_workspace.get(
                item.workspace_id, "read"
            )
        return permission_cache[item_id]

    for item_id in item_by_id:
        resolve(item_id)

    readable_items = {
        item_id: item
        for item_id, item in item_by_id.items()
        if permission_allows(permission_cache.get(item_id), "read")
    }
    readable_permissions = {
        item_id: permission_cache[item_id] for item_id in readable_items
    }
    return AccessibleSearchContext(
        workspaces=workspace_names,
        items=readable_items,
        permissions=readable_permissions,
    )


def _item_deep_link(item: WorkspaceItem) -> str:
    if item.type == "table":
        return f"/workbench/{item.id}/{item.default_view_id or 'v1'}"
    if item.type == "dashboard":
        return f"/workbench/{item.id}"
    if item.type == "folder":
        return f"/?workspaceId={item.workspace_id}&folderId={item.id}"
    return f"/?workspaceId={item.workspace_id}"


def _search_structure(
    context: AccessibleSearchContext,
    *,
    keyword: str,
    entity_types: Sequence[str],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    if "workspace" in entity_types:
        for workspace_id, workspace_name in context.workspaces.items():
            score = _score_name(workspace_name, keyword)
            if score is None:
                continue
            results.append(
                {
                    "entityType": "workspace",
                    "entityId": workspace_id,
                    "title": workspace_name,
                    "subtitle": "Workspace",
                    "snippet": workspace_name,
                    "workspace": {"id": workspace_id, "name": workspace_name},
                    "table": None,
                    "deepLink": f"/?workspaceId={workspace_id}",
                    "score": score,
                    "matchedFields": ["name"],
                    "sourceService": "qtable",
                }
            )

    for item in context.items.values():
        if item.type not in entity_types:
            continue
        score = _score_name(item.name, keyword)
        if score is None:
            continue
        workspace_name = context.workspaces.get(item.workspace_id, item.workspace_id)
        results.append(
            {
                "entityType": item.type,
                "entityId": item.id,
                "title": item.name,
                "subtitle": workspace_name,
                "snippet": item.name,
                "workspace": {"id": item.workspace_id, "name": workspace_name},
                "table": (
                    {
                        "id": item.id,
                        "name": item.name,
                        "defaultViewId": item.default_view_id,
                    }
                    if item.type == "table"
                    else None
                ),
                "deepLink": _item_deep_link(item),
                "score": score,
                "matchedFields": ["name"],
                "sourceService": "qtable",
            }
        )
    return results


def _record_field_expression(field_id: str) -> ColumnElement[Any]:
    # SQLAlchemy emits dialect-appropriate JSON extraction for SQLite and
    # PostgreSQL. Casting the extracted scalar avoids matching JSON object keys.
    return func.lower(cast(TableRecord.data[field_id].as_string(), String))


def _literal_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _field_contains_expression(field_id: str, keyword: str) -> ColumnElement[bool]:
    escaped = _literal_like(keyword.casefold())
    return _record_field_expression(field_id).like(
        f"%{escaped}%",
        escape="\\",
    )


def _field_prefix_expression(field_id: str, keyword: str) -> ColumnElement[bool]:
    escaped = _literal_like(keyword.casefold())
    return _record_field_expression(field_id).like(
        f"{escaped}%",
        escape="\\",
    )


def _table_record_predicates(
    *,
    table_id: str,
    fields: Sequence[TableField],
    keyword: str,
) -> tuple[ColumnElement[bool], ColumnElement[Any]]:
    primary = fields[0]
    all_contains = [
        _field_contains_expression(field.id, keyword) for field in fields
    ]
    table_match = and_(
        TableRecord.table_id == table_id,
        or_(*all_contains),
    )

    primary_expr = _record_field_expression(primary.id)
    exact_expr = primary_expr == keyword.casefold()
    prefix_expr = _field_prefix_expression(primary.id, keyword)
    contains_expr = _field_contains_expression(primary.id, keyword)

    other_contains = (
        or_(
            *[
                _field_contains_expression(field.id, keyword)
                for field in fields[1:]
            ]
        )
        if len(fields) > 1
        else None
    )
    base_score = case(
        (and_(TableRecord.table_id == table_id, exact_expr), 90),
        (and_(TableRecord.table_id == table_id, prefix_expr), 75),
        (and_(TableRecord.table_id == table_id, contains_expr), 60),
        else_=0,
    )
    extra_score = (
        case(
            (
                and_(
                    TableRecord.table_id == table_id,
                    other_contains,
                ),
                10,
            ),
            else_=0,
        )
        if other_contains is not None
        else 0
    )
    return table_match, base_score + extra_score


async def _search_records(
    db: AsyncSession,
    *,
    context: AccessibleSearchContext,
    keyword: str,
    required_visible_count: int,
    user_id: int,
) -> tuple[List[Dict[str, Any]], bool]:
    table_items = {
        item_id: item
        for item_id, item in context.items.items()
        if item.type == "table"
    }
    if not table_items or required_visible_count <= 0:
        return [], False

    field_result = await db.execute(
        select(TableField)
        .where(
            TableField.table_id.in_(list(table_items)),
            TableField.type.in_(sorted(TEXT_SEARCH_FIELD_TYPES)),
        )
        .order_by(TableField.table_id, TableField.order_index, TableField.id)
    )
    fields_by_table: Dict[str, List[TableField]] = {}
    for field in field_result.scalars().all():
        fields_by_table.setdefault(field.table_id, []).append(field)
    fields_by_table = {
        table_id: fields
        for table_id, fields in fields_by_table.items()
        if fields and table_id in table_items
    }
    if not fields_by_table:
        return [], False

    policy_result = await db.execute(
        select(TableRowPermissionPolicy).where(
            TableRowPermissionPolicy.table_id.in_(list(fields_by_table))
        )
    )
    policy_by_table = {
        policy.table_id: {
            "mode": policy.mode,
            "memberFieldId": (
                policy.member_field_id if policy.mode == "member_field" else None
            ),
            "enabled": policy.mode != "all",
        }
        for policy in policy_result.scalars().all()
    }

    predicates: List[ColumnElement[bool]] = []
    score_parts: List[ColumnElement[Any]] = []
    for table_id, fields in fields_by_table.items():
        predicate, score = _table_record_predicates(
            table_id=table_id,
            fields=fields,
            keyword=keyword,
        )
        predicates.append(predicate)
        score_parts.append(score)

    score_expr: ColumnElement[Any] = score_parts[0]
    for part in score_parts[1:]:
        score_expr = score_expr + part

    visible_results: List[Dict[str, Any]] = []
    candidate_offset = 0
    scanned = 0
    exhausted = False

    while (
        len(visible_results) < required_visible_count
        and scanned < MAX_RECORD_CANDIDATES_SCANNED
    ):
        chunk = min(
            RECORD_CANDIDATE_CHUNK,
            MAX_RECORD_CANDIDATES_SCANNED - scanned,
        )
        result = await db.execute(
            select(TableRecord, score_expr.label("search_score"))
            .where(or_(*predicates))
            .order_by(
                desc(score_expr),
                TableRecord.table_id.asc(),
                TableRecord.order_index.asc(),
                TableRecord.id.asc(),
            )
            .offset(candidate_offset)
            .limit(chunk)
        )
        rows = list(result.all())
        if not rows:
            exhausted = True
            break

        candidate_offset += len(rows)
        scanned += len(rows)

        for record, sql_score in rows:
            table_id = record.table_id
            permission = context.permissions.get(table_id)
            if not permission:
                continue
            policy = policy_by_table.get(
                table_id,
                {"mode": "all", "memberFieldId": None, "enabled": False},
            )
            if not record_is_visible(
                policy=policy,
                user_id=user_id,
                table_permission=permission,
                created_by_user_id=record.created_by_user_id,
                data=dict(record.data or {}),
            ):
                continue

            fields = fields_by_table[table_id]
            data = dict(record.data or {})
            matches: List[Dict[str, str]] = []
            snippets: List[str] = []
            query = keyword.casefold()
            for field in fields:
                value = _display_value(data.get(field.id))
                if value and query in value.casefold():
                    matches.append({"id": field.id, "name": field.name})
                    snippets.append(value)

            # SQL and Python use the same text field set, so this should only
            # be empty for dialect/legacy edge cases. Fail closed rather than
            # returning an unexplained candidate.
            if not matches:
                continue

            primary_value = _display_value(data.get(fields[0].id))
            title = primary_value or snippets[0] or record.id
            item = table_items[table_id]
            workspace_name = context.workspaces.get(
                item.workspace_id, item.workspace_id
            )
            visible_results.append(
                {
                    "entityType": "record",
                    "entityId": record.id,
                    "title": title,
                    "subtitle": f"{item.name} · {workspace_name}",
                    "snippet": _snippet(" · ".join(snippets), keyword),
                    "workspace": {
                        "id": item.workspace_id,
                        "name": workspace_name,
                    },
                    "table": {
                        "id": item.id,
                        "name": item.name,
                        "defaultViewId": item.default_view_id,
                    },
                    "deepLink": (
                        f"/workbench/{item.id}/{item.default_view_id or 'v1'}"
                        f"?recordId={record.id}"
                    ),
                    "score": int(sql_score or 0),
                    "matchedFields": matches,
                    "sourceService": "qtable",
                }
            )
            if len(visible_results) >= required_visible_count:
                break

        if len(rows) < chunk:
            exhausted = True
            break

    truncated = (
        not exhausted
        and scanned >= MAX_RECORD_CANDIDATES_SCANNED
        and len(visible_results) < required_visible_count
    )
    return visible_results, truncated


async def global_search(
    db: AsyncSession,
    *,
    user_id: int,
    keyword: str,
    workspace_id: Optional[str] = None,
    entity_types: Optional[Sequence[str]] = None,
    cursor: Optional[str] = None,
    limit: int = DEFAULT_GLOBAL_SEARCH_LIMIT,
) -> Dict[str, Any]:
    keyword = _normalize_keyword(keyword)
    normalized_types = _normalize_entity_types(entity_types)
    safe_limit = max(1, min(MAX_GLOBAL_SEARCH_LIMIT, int(limit)))
    fingerprint = _cursor_fingerprint(
        keyword=keyword,
        workspace_id=workspace_id,
        entity_types=normalized_types,
    )
    offset = _decode_cursor(cursor, fingerprint)

    context = await _load_accessible_context(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    if not context.workspaces:
        return {
            "query": keyword,
            "results": [],
            "nextCursor": None,
            "hasMore": False,
            "sourceServices": ["qtable"],
            "truncated": False,
        }

    structure_results = _search_structure(
        context,
        keyword=keyword,
        entity_types=normalized_types,
    )

    record_results: List[Dict[str, Any]] = []
    truncated = False
    if "record" in normalized_types:
        # Fetch only enough visible record hits to satisfy this combined page.
        # Candidate rows are keyword-filtered in SQL first; row permission
        # checks operate only on those candidates, never the entire table.
        required_records = offset + safe_limit + 1
        record_results, truncated = await _search_records(
            db,
            context=context,
            keyword=keyword,
            required_visible_count=required_records,
            user_id=user_id,
        )

    combined = [*structure_results, *record_results]
    combined.sort(
        key=lambda item: (
            -int(item.get("score") or 0),
            str(item.get("title") or "").casefold(),
            str(item.get("entityType") or ""),
            str(item.get("entityId") or ""),
        )
    )

    page = combined[offset : offset + safe_limit]
    has_more = len(combined) > offset + safe_limit
    next_cursor = (
        _encode_cursor(offset + len(page), fingerprint)
        if has_more and page
        else None
    )
    return {
        "query": keyword,
        "results": page,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "sourceServices": ["qtable"],
        "truncated": truncated,
    }
