from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableRecord, WorkspaceItem
from app.models.task_profile import TableTaskProfile
from app.models.user import User
from app.models.workspace_member import WorkspaceMember
from app.services.row_permissions import get_row_permission_policy, record_is_visible
from app.services.workspace import get_effective_permission_for_item, permission_allows

MAX_COMMENT_BODY = 20_000
MAX_PAGE_SIZE = 100
MAX_CURSOR_OFFSET = 100_000
_DANGEROUS_LINK_RE = re.compile(
    r"(\]\(\s*)(?:javascript|vbscript|data)\s*:",
    flags=re.IGNORECASE,
)


class CollaborationError(ValueError):
    pass


class CollaborationConflictError(CollaborationError):
    pass


class CollaborationNotFoundError(CollaborationError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def safe_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CollaborationError("limit must be an integer") from exc
    return max(1, min(MAX_PAGE_SIZE, parsed))


def encode_offset_cursor(offset: int) -> str:
    raw = json.dumps({"o": max(0, int(offset))}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_offset_cursor(value: Optional[str]) -> int:
    if not value:
        return 0
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset = int(payload.get("o", 0))
    except Exception as exc:
        raise CollaborationError("Invalid cursor") from exc
    if offset < 0 or offset > MAX_CURSOR_OFFSET:
        raise CollaborationError("Cursor is outside the supported window")
    return offset


def page_info(offset: int, returned: int, total: int) -> Dict[str, Any]:
    has_more = offset + returned < total
    return {
        "offset": offset,
        "hasMore": has_more,
        "nextCursor": encode_offset_cursor(offset + returned) if has_more else None,
    }


def sanitize_markdown(value: str) -> str:
    """Store Markdown as inert text while preserving normal Markdown syntax.

    Raw HTML is escaped and dangerous URL schemes inside Markdown links are
    neutralized. The frontend must still use a safe Markdown renderer; the
    backend never returns pre-rendered HTML.
    """
    body = str(value or "").strip()
    if not body:
        raise CollaborationError("Comment body is required")
    if len(body) > MAX_COMMENT_BODY:
        raise CollaborationError(f"Comment body exceeds {MAX_COMMENT_BODY} characters")
    body = html.escape(body, quote=False)
    return _DANGEROUS_LINK_RE.sub(r"\1#", body)


def normalize_user_ids(values: Optional[Sequence[Any]]) -> list[int]:
    output: list[int] = []
    seen: set[int] = set()
    for raw in values or []:
        if isinstance(raw, bool):
            continue
        try:
            user_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise CollaborationError("mention user IDs must be integers") from exc
        if user_id <= 0 or user_id in seen:
            continue
        seen.add(user_id)
        output.append(user_id)
    if len(output) > 50:
        raise CollaborationError("A comment may mention at most 50 members")
    return output


def member_value_contains_user(value: Any, user_id: int) -> bool:
    expected = str(user_id)
    if isinstance(value, list):
        return any(member_value_contains_user(item, user_id) for item in value)
    if isinstance(value, Mapping):
        for key in ("id", "userId", "user_id", "value"):
            candidate = value.get(key)
            if candidate is not None and str(candidate) == expected:
                return True
        return False
    if value is None or isinstance(value, bool):
        return False
    return str(value) == expected


async def table_item(db: AsyncSession, table_id: str) -> WorkspaceItem:
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == str(table_id),
            WorkspaceItem.type == "table",
        ).limit(1)
    )
    item = result.scalars().first()
    if item is None:
        raise CollaborationNotFoundError("Table not found")
    return item


async def workspace_members_by_id(
    db: AsyncSession,
    workspace_id: str,
    user_ids: Optional[Iterable[int]] = None,
) -> Dict[int, User]:
    statement = (
        select(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == workspace_id)
    )
    ids = {int(value) for value in (user_ids or [])}
    if ids:
        statement = statement.where(User.id.in_(list(ids)))
    result = await db.execute(statement.order_by(User.name.asc(), User.id.asc()))
    return {int(user.id): user for user in result.scalars().all()}


async def validate_workspace_members(
    db: AsyncSession,
    workspace_id: str,
    user_ids: Sequence[int],
) -> Dict[int, User]:
    if not user_ids:
        return {}
    members = await workspace_members_by_id(db, workspace_id, user_ids)
    missing = sorted(set(user_ids) - set(members))
    if missing:
        raise CollaborationError(
            "Mention recipients must be current workspace members: "
            + ", ".join(str(value) for value in missing)
        )
    return members


def actor_payload(user: Optional[User], user_id: Optional[int]) -> Dict[str, Any]:
    return {
        "id": user_id,
        "name": (
            user.name
            if user and user.name
            else user.email
            if user and user.email
            else "已离开成员"
            if user_id is not None
            else "系统"
        ),
        "email": user.email if user else None,
    }


async def actor_map(db: AsyncSession, user_ids: Iterable[Optional[int]]) -> Dict[int, User]:
    ids = {int(value) for value in user_ids if value is not None}
    if not ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(list(ids))))
    return {int(user.id): user for user in result.scalars().all()}


async def record_visibility(
    db: AsyncSession,
    *,
    user_id: int,
    table_id: str,
    record_id: str,
) -> tuple[bool, Optional[WorkspaceItem], Optional[TableRecord], Optional[str]]:
    try:
        item = await table_item(db, table_id)
    except CollaborationNotFoundError:
        return False, None, None, None
    permission = await get_effective_permission_for_item(db, user_id, table_id)
    if not permission_allows(permission, "read"):
        return False, item, None, permission
    result = await db.execute(
        select(TableRecord).where(
            TableRecord.table_id == table_id,
            TableRecord.id == str(record_id),
        ).limit(1)
    )
    record = result.scalars().first()
    if record is None:
        return False, item, None, permission
    policy = await get_row_permission_policy(db, table_id)
    visible = record_is_visible(
        policy=policy,
        user_id=user_id,
        table_permission=str(permission),
        created_by_user_id=record.created_by_user_id,
        data=dict(record.data or {}),
    )
    return visible, item, record if visible else None, permission


async def record_title(
    db: AsyncSession,
    *,
    table_id: str,
    record: TableRecord,
) -> str:
    result = await db.execute(
        select(TableTaskProfile).where(TableTaskProfile.table_id == table_id).limit(1)
    )
    profile = result.scalars().first()
    if profile and isinstance(profile.config, Mapping):
        title_field_id = profile.config.get("titleFieldId")
        if title_field_id:
            raw = (record.data or {}).get(str(title_field_id))
            if raw not in (None, ""):
                return str(raw)[:240]
    return "记录"


async def record_deep_link(
    db: AsyncSession,
    *,
    table: WorkspaceItem,
    record_id: str,
    comment_id: Optional[str] = None,
) -> str:
    view_id = str(table.default_view_id or "")
    base = f"/workbench/{quote(str(table.id), safe='')}"
    if view_id:
        base += f"/{quote(view_id, safe='')}"
    query = f"recordId={quote(str(record_id), safe='')}"
    if comment_id:
        query += f"&commentId={quote(str(comment_id), safe='')}"
    return f"{base}?{query}"
