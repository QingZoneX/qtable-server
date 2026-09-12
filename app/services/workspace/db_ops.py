"""
Workspace database backend operations
"""
from typing import Any, Dict, List, Optional, Tuple
import time

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.models.smart_table import WorkspaceItem
from .permissions import (
    normalize_permission,
    permission_allows,
    role_permission,
    _get_workspace_member,
    _get_user_permission_overrides,
    _resolve_permission_for_item,
)
from .helpers import (
    _normalize_workspace_id,
    get_default_workspace_id,
    _gen_id,
)


async def _ensure_workspace_db(
    db: AsyncSession,
    workspace_id: Optional[str] = None,
    workspace_name: Optional[str] = None,
) -> WorkspaceItem:
    """Ensure workspace exists in database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    
    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id).limit(1)
    )
    workspace = result.scalars().first()
    workspace_added = False
    
    if not workspace:
        workspace = Workspace(id=workspace_id, name=workspace_name or "默认空间")
        db.add(workspace)
        workspace_added = True
    
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.workspace_id == workspace_id,
            WorkspaceItem.parent_id.is_(None),
        ).limit(1)
    )
    root = result.scalars().first()
    
    if root:
        if workspace_added:
            await db.commit()
        return root
    
    root_id = _gen_id("fld")
    root = WorkspaceItem(
        id=root_id,
        workspace_id=workspace_id,
        type="folder",
        name=workspace_name or "默认空间",
        parent_id=None,
        order_index=0,
        default_view_id=None,
    )
    
    table_id = _gen_id("dst")
    table = WorkspaceItem(
        id=table_id,
        workspace_id=workspace_id,
        type="table",
        name="Projects",
        parent_id=root_id,
        order_index=0,
        default_view_id="v1",
    )
    
    db.add(root)
    db.add(table)
    try:
        # Keep workspace metadata and the table's core schema in one
        # transaction. Older code committed the WorkspaceItem first, so a
        # template initialization error could leave a visible zero-field table.
        await db.flush()
        from ..smart_table_store import init_table_db

        await init_table_db(db, table_id)
    except Exception:
        await db.rollback()
        raise

    return root


async def ensure_user_default_workspace(
    db: AsyncSession,
    user_id: int,
    user_name: Optional[str] = None,
) -> str:
    """Ensure user has a default workspace"""
    result = await db.execute(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
    )
    members = result.scalars().all()
    
    owner_membership = next(
        (member for member in members if member.role == WorkspaceRole.owner),
        None,
    )
    
    if owner_membership:
        return owner_membership.workspace_id
    
    workspace_id = get_default_workspace_id(user_id)
    await _ensure_workspace_db(db, workspace_id, user_name or "我的空间")
    
    existing = next(
        (member for member in members if member.workspace_id == workspace_id),
        None,
    )
    
    if not existing:
        db.add(
            WorkspaceMember(
                user_id=user_id,
                workspace_id=workspace_id,
                role=WorkspaceRole.owner,
            )
        )
        await db.commit()
    
    return workspace_id


async def list_workspace_db(
    db: AsyncSession,
    workspace_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Get workspace structure from database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)
    
    result = await db.execute(
        select(WorkspaceItem).where(WorkspaceItem.workspace_id == workspace_id)
    )
    items = result.scalars().all()
    
    permission_map: Dict[str, str] = {}
    visible_ids: Optional[set[str]] = None
    
    if user_id is not None:
        member = await _get_workspace_member(db, user_id, workspace_id)
        if member:
            overrides = await _get_user_permission_overrides(db, workspace_id, user_id)
            parent_map = {entry.id: entry.parent_id for entry in items}
            memo: Dict[str, str] = {}
            permission_map = {
                entry.id: _resolve_permission_for_item(
                    entry.id, parent_map, overrides, role_permission(member.role), memo
                )
                for entry in items
            }
            readable_ids = {
                entry.id
                for entry in items
                if permission_allows(permission_map.get(entry.id), "read")
            }
            visible_ids = set(readable_ids)
            for entry in items:
                if entry.id not in readable_ids:
                    continue
                parent_id = entry.parent_id
                while parent_id:
                    if parent_id in visible_ids:
                        break
                    visible_ids.add(parent_id)
                    parent_id = parent_map.get(parent_id)
        else:
            visible_ids = set()
    
    nodes: Dict[str, Dict[str, Any]] = {}
    children_map: Dict[Optional[str], List[WorkspaceItem]] = {}
    
    for item in items:
        if visible_ids is not None and item.id not in visible_ids:
            continue
        children_map.setdefault(item.parent_id, []).append(item)
    
    for parent_id in children_map:
        children_map[parent_id].sort(key=lambda x: x.order_index)
    
    for item in items:
        if visible_ids is not None and item.id not in visible_ids:
            continue
        if item.type == "table":
            nodes[item.id] = {
                "type": "table",
                "id": item.id,
                "name": item.name,
                "defaultViewId": item.default_view_id or "v1",
            }
        elif item.type == "dashboard":
            nodes[item.id] = {
                "type": "dashboard",
                "id": item.id,
                "name": item.name,
            }
        else:
            nodes[item.id] = {
                "type": "folder",
                "id": item.id,
                "name": item.name,
                "children": [],
            }
    
    for parent_id, children in children_map.items():
        if parent_id is None:
            continue
        parent_node = nodes.get(parent_id)
        if not parent_node:
            continue
        for child in children:
            if child.id in nodes:
                parent_node.setdefault("children", []).append(nodes[child.id])
    
    root_item = next((i for i in items if i.parent_id is None), None)
    root_node = nodes.get(root_item.id) if root_item else None
    
    if not root_node:
        root_node = {"id": _gen_id("fld"), "name": "Root", "children": []}
    else:
        root_node = {
            "id": root_node.get("id"),
            "name": root_node.get("name"),
            "children": root_node.get("children", [])
        }
    
    return {"workspaceId": workspace_id, "root": root_node}


async def list_workspaces_db(db: AsyncSession) -> List[Dict[str, Any]]:
    """List all workspaces from database"""
    await _ensure_workspace_db(db, "wkbDefault")
    
    result = await db.execute(
        select(WorkspaceItem).where(WorkspaceItem.parent_id.is_(None))
    )
    roots = result.scalars().all()
    roots.sort(key=lambda x: x.order_index)
    
    return [
        {"id": root.workspace_id, "name": root.name, "rootId": root.id}
        for root in roots
    ]


async def list_user_workspaces_db(
    db: AsyncSession,
    user_id: int,
    user_name: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """List user's workspaces from database"""
    await ensure_user_default_workspace(db, user_id, user_name)
    
    result = await db.execute(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
    )
    memberships = result.scalars().all()
    
    workspace_ids = {member.workspace_id for member in memberships}
    
    for workspace_id in workspace_ids:
        await _ensure_workspace_db(db, workspace_id)
    
    result_roots = await db.execute(
        select(WorkspaceMember, WorkspaceItem)
        .join(WorkspaceItem, WorkspaceItem.workspace_id == WorkspaceMember.workspace_id)
        .where(
            WorkspaceMember.user_id == user_id,
            WorkspaceItem.parent_id.is_(None),
        )
    )
    
    owned: list[Tuple[int, Dict[str, Any]]] = []
    invited: list[Tuple[int, Dict[str, Any]]] = []
    
    for member, root in result_roots.all():
        payload = {"id": root.workspace_id, "name": root.name, "rootId": root.id}
        if member.role == WorkspaceRole.owner:
            owned.append((root.order_index or 0, payload))
        else:
            invited.append((root.order_index or 0, payload))
    
    owned.sort(key=lambda item: item[0])
    invited.sort(key=lambda item: item[0])
    
    return {
        "owned": [item[1] for item in owned],
        "invited": [item[1] for item in invited],
    }


async def create_folder_db(
    db: AsyncSession,
    name: str,
    parent_id: str,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create folder in database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)
    
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == parent_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    parent = result.scalars().first()
    
    if not parent or parent.type in {"table", "dashboard"}:
        raise ValueError("Invalid parent for folder")
    
    result_max = await db.execute(
        select(func.max(WorkspaceItem.order_index)).where(
            WorkspaceItem.parent_id == parent_id, WorkspaceItem.workspace_id == workspace_id
        )
    )
    max_order = result_max.scalar() or 0
    
    folder_id = _gen_id("fld")
    folder = WorkspaceItem(
        id=folder_id,
        workspace_id=workspace_id,
        type="folder",
        name=name,
        parent_id=parent_id,
        order_index=max_order + 1,
        default_view_id=None,
    )
    
    db.add(folder)
    await db.commit()
    
    return {"type": "folder", "id": folder_id, "name": name, "children": []}


async def create_table_db(
    db: AsyncSession,
    name: str,
    parent_id: str,
    default_view_id: str = "v1",
    workspace_id: Optional[str] = None,
    template_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Create table in database, optionally from a visible catalog template."""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)

    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == parent_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    parent = result.scalars().first()

    if not parent or parent.type in {"table", "dashboard"}:
        raise ValueError("Invalid parent for table")

    result_max = await db.execute(
        select(func.max(WorkspaceItem.order_index)).where(
            WorkspaceItem.parent_id == parent_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    max_order = result_max.scalar() or 0

    table_id = _gen_id("dst")
    template_snapshot = None
    resolved_default_view_id = default_view_id
    if template_id:
        from ..table_templates import resolve_template_for_use

        _, template_snapshot = await resolve_template_for_use(
            db,
            template_id,
            user_id=user_id,
            workspace_id=workspace_id,
            target_table_id=table_id,
        )
        template_views = template_snapshot.get("views") or []
        if template_views and isinstance(template_views[0], dict):
            resolved_default_view_id = str(
                template_views[0].get("id") or default_view_id
            )

    table = WorkspaceItem(
        id=table_id,
        workspace_id=workspace_id,
        type="table",
        name=name,
        parent_id=parent_id,
        order_index=max_order + 1,
        default_view_id=resolved_default_view_id,
    )

    db.add(table)
    try:
        # Flush makes the WorkspaceItem visible inside the current transaction
        # without making it durable yet. init_table_db commits the complete
        # schema and the template usage counter together.
        await db.flush()
        from ..smart_table_store import init_table_db

        await init_table_db(
            db,
            table_id,
            template_id if template_snapshot is None else None,
            template_snapshot=template_snapshot,
        )
    except Exception:
        await db.rollback()
        raise

    return {
        "type": "table",
        "id": table_id,
        "name": name,
        "defaultViewId": resolved_default_view_id,
    }


async def rename_item_db(
    db: AsyncSession,
    item_id: str,
    name: str,
    workspace_id: Optional[str] = None,
) -> bool:
    """Rename item in database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)
    
    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == item_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    item = result.scalars().first()
    
    if not item:
        return False
    
    item.name = name
    await db.commit()
    return True


async def create_dashboard_db(
    db: AsyncSession,
    name: str,
    parent_id: str,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create dashboard in database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)

    result = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == parent_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    parent = result.scalars().first()

    if not parent or parent.type in {"table", "dashboard"}:
        raise ValueError("Invalid parent for dashboard")

    result_max = await db.execute(
        select(func.max(WorkspaceItem.order_index)).where(
            WorkspaceItem.parent_id == parent_id, WorkspaceItem.workspace_id == workspace_id
        )
    )
    max_order = result_max.scalar() or 0

    dashboard_id = _gen_id("dsb")
    dashboard_item = WorkspaceItem(
        id=dashboard_id,
        workspace_id=workspace_id,
        type="dashboard",
        name=name,
        parent_id=parent_id,
        order_index=max_order + 1,
        default_view_id=None,
    )

    db.add(dashboard_item)
    await db.commit()
    from ..dashboard_store.db_backend import ensure_dashboard_db

    await ensure_dashboard_db(db, dashboard_id, workspace_id)
    return {"type": "dashboard", "id": dashboard_id, "name": name}


async def delete_item_db(
    db: AsyncSession,
    item_id: str,
    workspace_id: Optional[str] = None,
) -> bool:
    """Delete item from database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)
    
    result = await db.execute(
        select(WorkspaceItem).where(WorkspaceItem.workspace_id == workspace_id)
    )
    items = result.scalars().all()
    
    nodes = {item.id: item for item in items}
    
    if item_id not in nodes:
        return False
    
    children_map: Dict[Optional[str], List[str]] = {}
    for item in items:
        children_map.setdefault(item.parent_id, []).append(item.id)
    
    to_delete = []
    stack = [item_id]
    
    while stack:
        current = stack.pop()
        to_delete.append(current)
        for child_id in children_map.get(current, []):
            stack.append(child_id)
    
    for delete_id in to_delete:
        item = nodes.get(delete_id)
        if item and item.type == "table":
            await delete_table_data_db(db, item.id)
        if item and item.type == "dashboard":
            from ..dashboard_store.db_backend import delete_dashboard_db

            await delete_dashboard_db(db, item.id)
    
    await db.execute(delete(WorkspaceItem).where(WorkspaceItem.id.in_(to_delete)))
    await db.commit()
    return True


async def move_item_db(
    db: AsyncSession,
    item_id: str,
    new_parent_id: str,
    workspace_id: Optional[str] = None,
) -> bool:
    """Move item in database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)
    
    result_item = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == item_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    item = result_item.scalars().first()
    
    if not item:
        return False
    
    result_parent = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == new_parent_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    new_parent = result_parent.scalars().first()
    
    if not new_parent or new_parent.type in {"table", "dashboard"}:
        return False
    
    result_max = await db.execute(
        select(func.max(WorkspaceItem.order_index)).where(
            WorkspaceItem.parent_id == new_parent_id, WorkspaceItem.workspace_id == workspace_id
        )
    )
    max_order = result_max.scalar() or 0
    
    item.parent_id = new_parent_id
    item.order_index = max_order + 1
    await db.commit()
    return True


async def copy_table_db(
    db: AsyncSession,
    table_id: str,
    new_parent_id: str,
    new_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Copy table in database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)
    
    result_item = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == table_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    item = result_item.scalars().first()
    
    if not item or item.type != "table":
        return None
    
    result_parent = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == new_parent_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    new_parent = result_parent.scalars().first()
    
    if not new_parent or new_parent.type == "table":
        return None
    
    result_max = await db.execute(
        select(func.max(WorkspaceItem.order_index)).where(
            WorkspaceItem.parent_id == new_parent_id, WorkspaceItem.workspace_id == workspace_id
        )
    )
    max_order = result_max.scalar() or 0
    
    new_table_id = _gen_id("dst")
    new_item = WorkspaceItem(
        id=new_table_id,
        workspace_id=workspace_id,
        type="table",
        name=new_name or f"{item.name} Copy",
        parent_id=new_parent_id,
        order_index=max_order + 1,
        default_view_id=item.default_view_id or "v1",
    )
    
    db.add(new_item)
    await db.commit()
    
    from ..smart_table_store import copy_table_db as copy_table_data_db
    await copy_table_data_db(db, item.id, new_table_id)
    
    return {
        "type": "table",
        "id": new_table_id,
        "name": new_item.name,
        "defaultViewId": new_item.default_view_id or "v1",
    }


async def copy_dashboard_db(
    db: AsyncSession,
    dashboard_id: str,
    new_parent_id: str,
    new_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Copy dashboard in database"""
    workspace_id = _normalize_workspace_id(workspace_id)
    await _ensure_workspace_db(db, workspace_id)

    result_item = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == dashboard_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    item = result_item.scalars().first()

    if not item or item.type != "dashboard":
        return None

    result_parent = await db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.id == new_parent_id,
            WorkspaceItem.workspace_id == workspace_id,
        )
    )
    new_parent = result_parent.scalars().first()

    if not new_parent or new_parent.type in {"table", "dashboard"}:
        return None

    result_max = await db.execute(
        select(func.max(WorkspaceItem.order_index)).where(
            WorkspaceItem.parent_id == new_parent_id, WorkspaceItem.workspace_id == workspace_id
        )
    )
    max_order = result_max.scalar() or 0

    new_dashboard_id = _gen_id("dsb")
    new_item = WorkspaceItem(
        id=new_dashboard_id,
        workspace_id=workspace_id,
        type="dashboard",
        name=new_name or f"{item.name} Copy",
        parent_id=new_parent_id,
        order_index=max_order + 1,
        default_view_id=None,
    )

    db.add(new_item)
    await db.commit()

    from ..dashboard_store.db_backend import copy_dashboard_db as copy_dashboard_data_db

    await copy_dashboard_data_db(db, item.id, new_dashboard_id, workspace_id)
    return {
        "type": "dashboard",
        "id": new_dashboard_id,
        "name": new_item.name,
    }


async def delete_table_data_db(db: AsyncSession, table_id: str) -> None:
    """Delete table data from database"""
    from ..smart_table_store import delete_table_data_db as _delete_table_data
    await _delete_table_data(db, table_id)


async def create_workspace_db(db: AsyncSession, name: str) -> Dict[str, Any]:
    """Create workspace in database"""
    workspace_id = _gen_id("wkb")
    root = await _ensure_workspace_db(db, workspace_id, name)
    return {"id": workspace_id, "name": root.name, "rootId": root.id}


async def rename_workspace_db(db: AsyncSession, workspace_id: str, name: str) -> bool:
    """Rename workspace in database"""
    root = await _ensure_workspace_db(db, workspace_id)
    root.name = name
    await db.commit()
    return True


async def delete_workspace_db(db: AsyncSession, workspace_id: str) -> bool:
    """Delete workspace from database"""
    await _ensure_workspace_db(db, workspace_id)
    
    result = await db.execute(
        select(WorkspaceItem).where(WorkspaceItem.workspace_id == workspace_id)
    )
    items = result.scalars().all()
    
    if not items:
        return False
    
    for item in items:
        if item.type == "table":
            await delete_table_data_db(db, item.id)
    
    await db.execute(delete(WorkspaceItem).where(WorkspaceItem.workspace_id == workspace_id))
    await db.commit()
    return True
