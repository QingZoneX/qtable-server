"""
Workspace module - Modular interface
"""
from typing import Any, Dict, List, Optional
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .permissions import (
    normalize_permission,
    permission_allows,
    role_permission,
    get_effective_permission_for_item,
)
from .helpers import (
    _ensure_workspace,
    _save_workspace,
    _normalize_workspace_id,
    get_default_workspace_id,
    _find_node,
    _find_parent,
)
from .file_ops import (
    list_workspace,
    list_workspaces,
    create_folder,
    create_table,
    create_dashboard,
    rename_item,
    delete_item,
    move_item,
    copy_table,
    copy_dashboard,
)
from .db_ops import (
    ensure_user_default_workspace,
    _ensure_workspace_db,
    list_workspace_db,
    list_workspaces_db,
    list_user_workspaces_db,
    create_folder_db,
    create_table_db as _create_table_db,
    create_dashboard_db,
    rename_item_db,
    delete_item_db as _delete_item_db,
    move_item_db,
    copy_table_db as _copy_table_db,
    copy_dashboard_db,
    create_workspace_db,
    rename_workspace_db,
    delete_workspace_db as _delete_workspace_db,
)
from app.models.smart_table import WorkspaceItem
from app.models.task_profile import TableTaskProfile
from app.services.task_profile_copy import remap_copied_task_profile_relations
from app.services.task_profile_portable import install_profile_from_annotations


async def _install_created_table_profile_or_cleanup(
    db: AsyncSession,
    *,
    created: Dict[str, Any],
    workspace_id: Optional[str],
    user_id: Optional[int],
) -> None:
    table_id = str(created["id"])
    try:
        await install_profile_from_annotations(
            db,
            table_id=table_id,
            user_id=user_id,
            commit=True,
        )
    except Exception:
        await db.rollback()
        await _delete_item_db(db, table_id, _normalize_workspace_id(workspace_id))
        raise


async def create_table_db(
    db: AsyncSession,
    name: str,
    parent_id: str,
    default_view_id: str = "v1",
    workspace_id: Optional[str] = None,
    template_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    created = await _create_table_db(
        db,
        name,
        parent_id,
        default_view_id,
        workspace_id,
        template_id,
        user_id,
    )
    await _install_created_table_profile_or_cleanup(
        db,
        created=created,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return created


async def copy_table_db(
    db: AsyncSession,
    table_id: str,
    new_parent_id: str,
    new_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    created = await _copy_table_db(
        db,
        table_id,
        new_parent_id,
        new_name,
        workspace_id,
    )
    if created is not None:
        try:
            await remap_copied_task_profile_relations(
                db,
                source_table_id=table_id,
                target_table_id=str(created["id"]),
            )
            await db.commit()
            await _install_created_table_profile_or_cleanup(
                db,
                created=created,
                workspace_id=workspace_id,
                user_id=None,
            )
        except Exception:
            # Always recover the SQLAlchemy session before cleanup. The profile
            # installer may already have removed the destination, and deleting
            # a missing workspace item is intentionally a harmless no-op.
            await db.rollback()
            await _delete_item_db(
                db,
                str(created["id"]),
                _normalize_workspace_id(workspace_id),
            )
            raise
    return created


async def delete_item_db(
    db: AsyncSession,
    item_id: str,
    workspace_id: Optional[str] = None,
) -> bool:
    """Delete a workspace subtree and its Task Profile rows."""
    resolved_workspace = _normalize_workspace_id(workspace_id)
    result = await db.execute(
        select(WorkspaceItem).where(WorkspaceItem.workspace_id == resolved_workspace)
    )
    items = list(result.scalars().all())
    children: Dict[Optional[str], List[str]] = {}
    by_id = {item.id: item for item in items}
    for item in items:
        children.setdefault(item.parent_id, []).append(item.id)

    stack = [item_id]
    table_ids: List[str] = []
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        item = by_id.get(current)
        if item is not None and item.type == "table":
            table_ids.append(item.id)
        stack.extend(children.get(current, []))

    deleted = await _delete_item_db(db, item_id, resolved_workspace)
    if deleted and table_ids:
        await db.execute(
            delete(TableTaskProfile).where(TableTaskProfile.table_id.in_(table_ids))
        )
        await db.commit()
    return deleted


async def delete_workspace_db(db: AsyncSession, workspace_id: str) -> bool:
    """Delete a workspace without leaving Task Profile rows orphaned."""
    result = await db.execute(
        select(WorkspaceItem.id).where(
            WorkspaceItem.workspace_id == workspace_id,
            WorkspaceItem.type == "table",
        )
    )
    table_ids = list(result.scalars().all())
    deleted = await _delete_workspace_db(db, workspace_id)
    if deleted and table_ids:
        await db.execute(
            delete(TableTaskProfile).where(TableTaskProfile.table_id.in_(table_ids))
        )
        await db.commit()
    return deleted


__all__ = [
    "normalize_permission",
    "permission_allows",
    "role_permission",
    "get_effective_permission_for_item",
    "_ensure_workspace",
    "_save_workspace",
    "_normalize_workspace_id",
    "get_default_workspace_id",
    "_find_node",
    "_find_parent",
    "list_workspace",
    "list_workspaces",
    "create_folder",
    "create_table",
    "create_dashboard",
    "rename_item",
    "delete_item",
    "move_item",
    "copy_table",
    "copy_dashboard",
    "ensure_user_default_workspace",
    "_ensure_workspace_db",
    "list_workspace_db",
    "list_workspaces_db",
    "list_user_workspaces_db",
    "create_folder_db",
    "create_table_db",
    "create_dashboard_db",
    "rename_item_db",
    "delete_item_db",
    "move_item_db",
    "copy_table_db",
    "copy_dashboard_db",
    "create_workspace_db",
    "rename_workspace_db",
    "delete_workspace_db",
]
