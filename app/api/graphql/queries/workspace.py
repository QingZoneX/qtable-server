from typing import Dict, List, Optional

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.api.graphql.helpers import (
    _resolve_backend,
    _resolve_db_table_id,
    _table_exists,
    _require_item_permission,
    _require_user,
    _resolve_workspace_for_user,
    _require_workspace_exists,
    _require_workspace_member,
    ensure_user_default_workspace,
)
from app.api.graphql.types import WorkspaceMemberInfo, WorkspaceItemAccessInfo
from app.models.user import User
from app.models.workspace_member import WorkspaceMember
from app.models.smart_table import WorkspaceItem, WorkspaceItemPermission
from app.services.workspace import get_effective_permission_for_item, normalize_permission, permission_allows, role_permission, list_workspace, list_workspace_db, list_workspaces, list_user_workspaces_db
from app.services.smart_table_store.helpers import get_template_index
from app.services.table_templates import (
    can_manage_custom_template,
    get_visible_template,
    list_visible_templates,
    serialize_template,
)


@strawberry.type
class WorkspaceQueries:
    """Mixin for workspace-related query resolvers"""
    
    @strawberry.field
    async def workspace(self, info: Info, workspace_id: Optional[str] = None) -> JSON:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            user = await _require_user(info)
            resolved_workspace_id = workspace_id or await ensure_user_default_workspace(
                db, user.id, user.name
            )
            await _require_workspace_member(db, user.id, resolved_workspace_id)
            return await list_workspace_db(db, resolved_workspace_id, user.id)
        return list_workspace(workspace_id)

    @strawberry.field
    async def workspaces(self, info: Info) -> JSON:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            user = await _require_user(info)
            return await list_user_workspaces_db(db, user.id, user.name)
        return {"owned": list_workspaces(), "invited": []}

    @strawberry.field(name="workspaceMembers")
    async def workspaceMembers(self, info: Info, workspace_id: strawberry.ID) -> List[WorkspaceMemberInfo]:
        if _resolve_backend(None) != "db":
            try:
                user = await _require_user(info)
                return [WorkspaceMemberInfo(
                    user_id=user.id,
                    name=user.name,
                    email=user.email,
                    role="owner"
                )]
            except:
                return []
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        workspace_id_value = str(workspace_id)
        await _require_workspace_exists(db, workspace_id_value)
        await _require_workspace_member(db, user.id, workspace_id_value)
        result = await db.execute(
            select(User, WorkspaceMember)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .where(WorkspaceMember.workspace_id == workspace_id_value)
        )
        rows = result.all()
        return [
            WorkspaceMemberInfo(
                user_id=user.id,
                name=user.name,
                email=user.email,
                role=member.role.value if hasattr(member.role, "value") else str(member.role),
            )
            for user, member in rows
        ]

    @strawberry.field(name="itemAccess")
    async def itemAccess(
        self,
        info: Info,
        item_id: str,
        workspace_id: Optional[str] = None,
    ) -> List[WorkspaceItemAccessInfo]:
        if _resolve_backend(None) != "db":
            try:
                user = await _require_user(info)
                return [WorkspaceItemAccessInfo(
                    user_id=user.id,
                    name=user.name,
                    email=user.email,
                    role="owner",
                    permission="manage",
                    inherited=False
                )]
            except:
                return []
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        resolved_workspace_id = workspace_id or await ensure_user_default_workspace(
            db, user.id, user.name
        )
        await _require_workspace_exists(db, resolved_workspace_id)
        await _require_workspace_member(db, user.id, resolved_workspace_id)
        result_item = await db.execute(
            select(WorkspaceItem).where(
                WorkspaceItem.id == item_id,
                WorkspaceItem.workspace_id == resolved_workspace_id,
            )
        )
        item = result_item.scalars().first()
        if not item:
            result_root = await db.execute(
                select(WorkspaceItem).where(
                    WorkspaceItem.workspace_id == resolved_workspace_id,
                    WorkspaceItem.parent_id.is_(None),
                ).limit(1)
            )
            item = result_root.scalars().first()
        if not item:
            return []
        await _require_item_permission(info, item.id, "read")
        result_members = await db.execute(
            select(User, WorkspaceMember)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .where(WorkspaceMember.workspace_id == resolved_workspace_id)
        )
        member_rows = result_members.all()
        result_items = await db.execute(
            select(WorkspaceItem).where(WorkspaceItem.workspace_id == resolved_workspace_id)
        )
        items = result_items.scalars().all()
        parent_map = {entry.id: entry.parent_id for entry in items}
        chain_ids: List[str] = []
        current_id: Optional[str] = item.id
        while current_id:
            chain_ids.append(current_id)
            current_id = parent_map.get(current_id)
        result_overrides = await db.execute(
            select(WorkspaceItemPermission).where(
                WorkspaceItemPermission.item_id.in_(chain_ids)
            )
        )
        overrides = result_overrides.scalars().all()
        overrides_by_item: Dict[str, Dict[int, str]] = {}
        for entry in overrides:
            overrides_by_item.setdefault(entry.item_id, {})[entry.user_id] = normalize_permission(
                entry.permission
            )
        entries: List[WorkspaceItemAccessInfo] = []
        for member_user, member in member_rows:
            role_perm = role_permission(member.role)
            current = item.id
            permission = role_perm
            inherited = True
            while current:
                item_overrides = overrides_by_item.get(current)
                if item_overrides and member_user.id in item_overrides:
                    permission = item_overrides[member_user.id]
                    inherited = current != item.id
                    break
                current = parent_map.get(current)
            entries.append(
                WorkspaceItemAccessInfo(
                    user_id=member_user.id,
                    name=member_user.name,
                    email=member_user.email,
                    role=member.role.value if hasattr(member.role, "value") else str(member.role),
                    permission=permission,
                    inherited=inherited,
                )
            )
        return entries

    @strawberry.field
    async def templates(
        self,
        info: Info,
        workspace_id: Optional[str] = None,
        scope: Optional[str] = None,
        search: Optional[str] = None,
        category: Optional[str] = None,
        include_archived: bool = False,
    ) -> JSON:
        """List templates visible to the current user.

        File backend keeps the legacy static catalog. Database backend is
        catalog-driven and applies personal/workspace visibility server-side.
        """
        if _resolve_backend(None) != "db":
            return get_template_index()
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        if workspace_id:
            await _require_workspace_exists(db, workspace_id)
            await _require_workspace_member(db, user.id, workspace_id)
        return await list_visible_templates(
            db,
            user_id=user.id,
            workspace_id=workspace_id,
            scope=scope,
            search=search,
            category=category,
            include_archived=include_archived,
        )

    @strawberry.field(name="templateDetail")
    async def template_detail(
        self,
        info: Info,
        template_id: str,
        workspace_id: Optional[str] = None,
        include_archived: bool = False,
    ) -> JSON:
        """Return one visible template including its reusable snapshot."""
        if _resolve_backend(None) != "db":
            metadata = next(
                (
                    entry
                    for entry in get_template_index()
                    if str(entry.get("id")) == template_id
                ),
                None,
            )
            if metadata is None:
                raise ValueError("Template not found or no access")
            return metadata
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        template = await get_visible_template(
            db,
            template_id,
            user_id=user.id,
            workspace_id=workspace_id,
            include_archived=include_archived,
        )
        can_manage = (
            False
            if template.scope == "system"
            else await can_manage_custom_template(
                db,
                template,
                user_id=user.id,
            )
        )
        return serialize_template(
            template,
            include_snapshot=True,
            can_manage=can_manage,
        )

