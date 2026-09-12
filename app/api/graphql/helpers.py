import asyncio
from datetime import datetime, timedelta
import hashlib
import secrets
from typing import AsyncGenerator, List, Optional, Dict, Any

from graphql import GraphQLError
from strawberry.types import Info
from fastapi import Request, WebSocket
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import JWTError, decode_access_token
from app.db.session import AsyncSessionLocal
from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.models.workspace_member import WorkspaceMember, WorkspaceRole
from app.models.smart_table import WorkspaceItem, WorkspaceItemPermission
from app.services.smart_table_store import (
    get_full_store,
    get_full_store_for_table,
    resolve_table_id,
)
from app.services.workspace import (
    get_effective_permission_for_item,
    normalize_permission,
    permission_allows,
    role_permission,
    ensure_user_default_workspace,
)
from app.services.row_permissions import (
    filter_store_for_user,
    get_row_permission_policy,
    require_record_access,
)


# ===== Realtime Brokers =====

class RealtimeBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def publish(self, payload: Dict[str, Any]) -> None:
        async with self._lock:
            for queue in list(self._subscribers):
                queue.put_nowait(payload)

    async def subscribe(self) -> AsyncGenerator[Dict[str, Any], None]:  # type: ignore
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                payload = await queue.get()
                yield payload
        finally:
            async with self._lock:
                self._subscribers.discard(queue)


table_broker = RealtimeBroker()
yjs_broker = RealtimeBroker()
table_presence_broker = RealtimeBroker()


# ===== Table Presence Registry =====

class TablePresenceRegistry:
    def __init__(self, ttl_seconds: int = 15) -> None:
        self._ttl_seconds = ttl_seconds
        self._tables: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def _viewer_key(self, entry: Dict[str, Any]) -> str:
        user_id = entry.get("userId")
        if user_id is not None:
            return f"user:{user_id}"
        email = (entry.get("email") or "").strip().lower()
        if email:
            return f"email:{email}"
        return f"session:{entry.get('sessionId')}"

    def _build_viewers(self, table_entries: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        unique: Dict[str, Dict[str, Any]] = {}
        for entry in table_entries.values():
            key = self._viewer_key(entry)
            current = unique.get(key)
            if not current or entry["lastSeen"] > current["lastSeen"]:
                unique[key] = entry
        viewers = [
            {
                "userId": value.get("userId"),
                "name": value.get("name") or "Guest",
                "email": value.get("email"),
            }
            for value in unique.values()
        ]
        viewers.sort(key=lambda item: ((item.get("name") or "").lower(), (item.get("email") or "").lower()))
        return viewers

    async def touch(self, table_id: str, session_id: str, viewer: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        now = datetime.utcnow()
        async with self._lock:
            changed_table_ids = self._prune_locked(now)
            table_entries = self._tables.setdefault(table_id, {})
            previous = table_entries.get(session_id)
            table_entries[session_id] = {
                "sessionId": session_id,
                "userId": viewer.get("userId"),
                "name": viewer.get("name") or "Guest",
                "email": viewer.get("email"),
                "lastSeen": now,
            }
            changed = previous is None or (
                previous.get("userId") != table_entries[session_id].get("userId")
                or previous.get("name") != table_entries[session_id].get("name")
                or previous.get("email") != table_entries[session_id].get("email")
            )
            if table_id in changed_table_ids:
                changed = True
            viewers = self._build_viewers(table_entries)
            event = {
                "tableId": table_id,
                "updatedAt": now.isoformat(),
                "viewers": viewers,
            }
            return event, changed

    async def remove(self, table_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        now = datetime.utcnow()
        async with self._lock:
            self._prune_locked(now)
            table_entries = self._tables.get(table_id)
            if not table_entries or session_id not in table_entries:
                return None
            table_entries.pop(session_id, None)
            if not table_entries:
                self._tables.pop(table_id, None)
                viewers: List[Dict[str, Any]] = []
            else:
                viewers = self._build_viewers(table_entries)
            return {
                "tableId": table_id,
                "updatedAt": now.isoformat(),
                "viewers": viewers,
            }

    async def get_viewers(self, table_id: str) -> List[Dict[str, Any]]:
        now = datetime.utcnow()
        async with self._lock:
            self._prune_locked(now)
            table_entries = self._tables.get(table_id, {})
            return self._build_viewers(table_entries)

    async def prune_expired(self) -> List[Dict[str, Any]]:
        now = datetime.utcnow()
        events: List[Dict[str, Any]] = []
        async with self._lock:
            changed_table_ids = self._prune_locked(now)
            for table_id in changed_table_ids:
                table_entries = self._tables.get(table_id, {})
                events.append(
                    {
                        "tableId": table_id,
                        "updatedAt": now.isoformat(),
                        "viewers": self._build_viewers(table_entries),
                    }
                )
        return events

    def _prune_locked(self, now: datetime) -> set[str]:
        changed: set[str] = set()
        expired_before = now - timedelta(seconds=self._ttl_seconds)
        for table_id in list(self._tables.keys()):
            table_entries = self._tables.get(table_id, {})
            expired_sessions = [
                session_id
                for session_id, entry in table_entries.items()
                if entry.get("lastSeen") < expired_before
            ]
            for session_id in expired_sessions:
                table_entries.pop(session_id, None)
                changed.add(table_id)
            if not table_entries:
                if table_id in self._tables:
                    self._tables.pop(table_id, None)
                    changed.add(table_id)
        return changed


table_presence_registry = TablePresenceRegistry()
_presence_cleanup_task: Optional[asyncio.Task] = None
_presence_cleanup_lock = asyncio.Lock()


async def ensure_presence_cleanup_task() -> None:
    global _presence_cleanup_task
    async with _presence_cleanup_lock:
        if _presence_cleanup_task and not _presence_cleanup_task.done():
            return
        _presence_cleanup_task = asyncio.create_task(_presence_cleanup_loop())


async def _presence_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3)
        events = await table_presence_registry.prune_expired()
        for event in events:
            await table_presence_broker.publish(event)


# ===== Backend Resolution Helpers =====

def _resolve_table_id_for_backend(table_id: Optional[str]) -> str:
    if _resolve_backend(table_id) == "db":
        return _resolve_db_table_id(table_id)
    return table_id or "table"


def _resolve_backend(table_id: Optional[str]) -> str:
    backend = (settings.DATA_BACKEND or "").lower()
    if backend in {"db", "database", "postgres", "sqlite"}:
        return "db"
    if backend in {"file", "json"}:
        return "file"
    return "file"


def _resolve_db_table_id(table_id: Optional[str]) -> str:
    return table_id or "dstDefault"


# ===== Table Update Helpers =====

async def publish_table_update(db: AsyncSession, table_id: Optional[str] = None) -> Dict[str, Any]:
    """Publish DB invalidations without materializing the entire record set."""
    backend = _resolve_backend(table_id)
    resolved_table_id = _resolve_table_id_for_backend(table_id)
    payload = await get_full_store_for_table(table_id) if backend == "file" else None
    event_payload = {
        "tableId": resolved_table_id,
        "updatedAt": datetime.utcnow().isoformat(),
        "data": payload,
        "snapshotIncluded": payload is not None,
    }
    await table_broker.publish(event_payload)
    try:
        from app.services.dashboard_cache import get_dashboard_cache

        cache = get_dashboard_cache()
        await cache.invalidate_table(resolved_table_id)
    except Exception:
        pass
    return event_payload


# ===== Context Getter =====

async def get_context(
    request: Request = None,
    websocket: WebSocket = None,
):
    async with AsyncSessionLocal() as db:
        headers = request.headers if request else (websocket.headers if websocket else {})
        auth_header = headers.get("Authorization", "")
        token = (
            auth_header.split(" ", 1)[1]
            if auth_header.lower().startswith("bearer ")
            else None
        )
        if not token and websocket:
            connection_params = websocket.scope.get("connection_params")
            if isinstance(connection_params, dict):
                auth_value = connection_params.get("Authorization") or connection_params.get("authorization")
                if isinstance(auth_value, str):
                    token = (
                        auth_value.split(" ", 1)[1]
                        if auth_value.lower().startswith("bearer ")
                        else auth_value
                    )
        user = None
        if token:
            try:
                payload = decode_access_token(token)
                user_id = payload.get("user_id")
                if user_id is not None:
                    result = await db.execute(select(User).where(User.id == user_id))
                    user = result.scalars().first()
            except JWTError:
                user = None
        yield {"db": db, "data_backend": settings.DATA_BACKEND, "user": user}


# ===== View Config Helpers =====

ALLOWED_VIEW_TYPES = {"grid", "board", "gantt", "calendar", "gallery"}
DEFAULT_VIEW_NAMES = {
    "grid": "Grid",
    "board": "Kanban",
    "gantt": "Gantt",
    "calendar": "Calendar",
    "gallery": "Gallery",
}
DEFAULT_TOOLBAR_ITEMS = {
    "grid": ["insertRow", "fields", "filter", "group", "sort", "automations", "share"],
    "board": ["group", "filter", "sort", "share"],
    "gantt": ["insertRow", "viewSettings", "fields", "filter", "group", "sort", "automations", "share"],
    "calendar": ["insertRow", "viewSettings", "fields", "filter", "sort", "share"],
    "gallery": ["insertRow", "viewSettings", "fields", "filter", "sort", "share"],
}


def _next_view_name(base_name: str, existing_names: List[str]) -> str:
    existing = {name.strip() for name in existing_names if isinstance(name, str)}
    if base_name not in existing:
        return base_name
    index = 2
    while f"{base_name} {index}" in existing:
        index += 1
    return f"{base_name} {index}"


def _build_default_view_config(view_type: str) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "toolbar": {"items": list(DEFAULT_TOOLBAR_ITEMS.get(view_type, DEFAULT_TOOLBAR_ITEMS["grid"]))},
        "filters": [],
        "sorts": [],
        "groupConfig": {"fieldId": None, "order": "asc"},
        "hiddenFieldIds": [],
    }
    if view_type == "gantt":
        config["ganttConfig"] = {
            "startFieldId": None,
            "endFieldId": None,
            "progressFieldId": None,
        }
    if view_type == "calendar":
        config["calendarConfig"] = {
            "startFieldId": None,
            "endFieldId": None,
            "weekStartsOn": 1,
        }
    if view_type == "gallery":
        config["galleryConfig"] = {
            "coverFieldId": None,
            "titleFieldId": None,
            "cardSize": "medium",
            "imageFit": "cover",
            "showFieldNames": True,
        }
    if view_type == "board":
        config["groupConfig"] = {"fieldId": None, "order": "asc"}
    return config


# ===== Permission Helpers =====

async def _table_exists(db: AsyncSession, table_id: str) -> bool:
    result = await db.execute(
        select(WorkspaceItem.id).where(WorkspaceItem.id == table_id).limit(1)
    )
    return result.scalars().first() is not None


async def _require_item_permission(
    info: Info,
    item_id: str,
    required: str,
) -> str:
    if _resolve_backend(None) != "db":
        return required
    
    # 对于默认表，放宽权限检查（开发环境）
    if item_id in ("dstDefault", "table"):
        return required
    
    db: AsyncSession = info.context["db"]
    user = await _require_user(info)
    permission = await get_effective_permission_for_item(db, user.id, item_id)
    if not permission_allows(permission, required):
        raise GraphQLError("No access")
    return permission


async def _require_user(info: Info) -> User:
    user = info.context.get("user")
    if not user:
        raise GraphQLError("Unauthorized")
    return user


async def _row_permission_context(
    info: Info,
    table_id: str,
    required: str = "read",
) -> tuple[User, str, Dict[str, Any]]:
    """Resolve table permission and row policy for the current user."""
    permission = await _require_item_permission(info, table_id, required)
    user = await _require_user(info)
    db: AsyncSession = info.context["db"]
    policy = await get_row_permission_policy(db, table_id)
    return user, permission, policy


async def _filter_store_by_row_permission(
    info: Info,
    table_id: str,
    store: Dict[str, Any],
    required: str = "read",
) -> tuple[Dict[str, Any], Dict[str, Any], str]:
    user, permission, _ = await _row_permission_context(
        info, table_id, required
    )
    db: AsyncSession = info.context["db"]
    filtered, policy = await filter_store_for_user(
        db,
        table_id,
        store,
        user_id=user.id,
        table_permission=permission,
    )
    return filtered, policy, permission


async def _require_record_permission(
    info: Info,
    table_id: str,
    record_id: str,
    required: str,
) -> str:
    """Enforce both table-level permission and row visibility for one record."""
    user, permission, _ = await _row_permission_context(
        info, table_id, required
    )
    db: AsyncSession = info.context["db"]
    try:
        await require_record_access(
            db,
            table_id,
            str(record_id),
            user_id=user.id,
            table_permission=permission,
        )
    except PermissionError as exc:
        raise GraphQLError("Record not found or no access") from exc
    return permission


def _presence_viewer_from_context(info: Info, session_id: str) -> Dict[str, Any]:
    user = info.context.get("user")
    if user:
        return {
            "userId": user.id,
            "name": user.name or user.email or "Guest",
            "email": user.email,
            "sessionId": session_id,
        }
    return {
        "userId": None,
        "name": f"Guest-{session_id[:4]}",
        "email": None,
        "sessionId": session_id,
    }


async def _resolve_workspace_for_user(
    info: Info,
    workspace_id: Optional[str],
) -> tuple[User, str, WorkspaceMember]:
    db: AsyncSession = info.context["db"]
    user = await _require_user(info)
    resolved_workspace_id = workspace_id or await ensure_user_default_workspace(
        db, user.id, user.name
    )
    await _require_workspace_exists(db, resolved_workspace_id)
    member = await _require_workspace_member(db, user.id, resolved_workspace_id)
    return user, resolved_workspace_id, member


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _create_reset_token(db: AsyncSession, user_id: int) -> str:
    await db.execute(
        delete(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
    )
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_reset_token(raw_token)
    expires_at = datetime.utcnow() + timedelta(
        minutes=settings.RESET_TOKEN_EXPIRE_MINUTES
    )
    db.add(
        PasswordResetToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    await db.commit()
    return raw_token


async def _require_workspace_member(db: AsyncSession, user_id: int, workspace_id: str) -> WorkspaceMember:
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    member = result.scalars().first()
    if not member:
        result = await db.execute(
            select(func.count()).where(WorkspaceMember.workspace_id == workspace_id)
        )
        member_count = result.scalar_one()
        if member_count == 0:
            member = WorkspaceMember(
                user_id=user_id,
                workspace_id=workspace_id,
                role=WorkspaceRole.owner,
            )
            db.add(member)
            await db.commit()
            return member
        raise ValueError("No access to workspace")
    return member


async def _require_workspace_exists(db: AsyncSession, workspace_id: str) -> None:
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.workspace_id == workspace_id,
            WorkspaceItem.parent_id.is_(None),
        ).limit(1)
    )
    if not result.scalars().first():
        raise ValueError("Workspace not found")
