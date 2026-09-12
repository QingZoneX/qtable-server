"""
Workspace permissions module
"""
from typing import Dict, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.models.smart_table import WorkspaceItem, WorkspaceItemPermission


PERMISSION_LEVELS = ["read", "update", "edit", "manage"]
PERMISSION_ORDER = {name: index for index, name in enumerate(PERMISSION_LEVELS)}
ROLE_PERMISSION = {
    WorkspaceRole.owner: "manage",
    WorkspaceRole.editor: "edit",
    WorkspaceRole.viewer: "read",
}


def normalize_permission(permission: Optional[str]) -> str:
    """Normalize permission to valid value"""
    if permission in PERMISSION_ORDER:
        return permission
    return "read"


def permission_allows(current: Optional[str], required: str) -> bool:
    """Check if current permission allows required permission"""
    if not current:
        return False
    return PERMISSION_ORDER.get(current, -1) >= PERMISSION_ORDER.get(required, 0)


def role_permission(role: WorkspaceRole) -> str:
    """Get permission level for a role"""
    return ROLE_PERMISSION.get(role, "read")


async def _get_workspace_member(
    db: AsyncSession, user_id: int, workspace_id: str
) -> Optional[WorkspaceMember]:
    """Get workspace member"""
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.workspace_id == workspace_id,
        )
    )
    return result.scalars().first()


async def _get_user_permission_overrides(
    db: AsyncSession, workspace_id: str, user_id: int
) -> Dict[str, str]:
    """Get user permission overrides for workspace items"""
    result = await db.execute(
        select(WorkspaceItemPermission)
        .join(WorkspaceItem, WorkspaceItemPermission.item_id == WorkspaceItem.id)
        .where(
            WorkspaceItem.workspace_id == workspace_id,
            WorkspaceItemPermission.user_id == user_id,
        )
    )
    overrides = result.scalars().all()
    return {item.item_id: normalize_permission(item.permission) for item in overrides}


def _resolve_permission_for_item(
    item_id: str,
    parent_map: Dict[str, Optional[str]],
    overrides: Dict[str, str],
    fallback_permission: str,
    memo: Dict[str, str],
) -> str:
    """Resolve permission for an item considering inheritance"""
    if item_id in memo:
        return memo[item_id]
    if item_id in overrides:
        memo[item_id] = overrides[item_id]
        return memo[item_id]
    parent_id = parent_map.get(item_id)
    if not parent_id:
        memo[item_id] = fallback_permission
        return memo[item_id]
    memo[item_id] = _resolve_permission_for_item(
        parent_id, parent_map, overrides, fallback_permission, memo
    )
    return memo[item_id]


async def get_effective_permission_for_item(
    db: AsyncSession, user_id: int, item_id: str
) -> Optional[str]:
    """Get effective permission for a user on an item"""
    result = await db.execute(select(WorkspaceItem).where(WorkspaceItem.id == item_id).limit(1))
    item = result.scalars().first()
    if not item:
        return None

    member = await _get_workspace_member(db, user_id, item.workspace_id)
    if not member:
        return None

    chain: list[str] = []
    current = item
    seen: set[str] = set()
    while current and current.id not in seen:
        seen.add(current.id)
        chain.append(current.id)
        if not current.parent_id:
            break
        next_result = await db.execute(
            select(WorkspaceItem).where(WorkspaceItem.id == current.parent_id).limit(1)
        )
        current = next_result.scalars().first()

    result_overrides = await db.execute(
        select(WorkspaceItemPermission).where(
            WorkspaceItemPermission.user_id == user_id,
            WorkspaceItemPermission.item_id.in_(chain),
        )
    )
    overrides = {row.item_id: normalize_permission(row.permission) for row in result_overrides.scalars().all()}
    for cid in chain:
        if cid in overrides:
            return overrides[cid]
    return role_permission(member.role)
