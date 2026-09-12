import secrets
from datetime import datetime, timedelta
from typing import List, Optional

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from starlette.concurrency import run_in_threadpool

from app.api.auth import _hash_reset_token, _send_reset_email, _smtp_configured
from app.api.graphql.helpers import (
    _resolve_backend,
    _resolve_db_table_id,
    _require_item_permission,
    _require_user,
    ensure_user_default_workspace,
    _resolve_workspace_for_user,
    _require_workspace_exists,
    _require_workspace_member,
    normalize_permission,
)
from app.api.graphql.types import WorkspaceMemberInfo
from app.core.config import settings
from app.core.security import hash_password
from app.models.user import User
from app.models.workspace_member import WorkspaceMember, WorkspaceRole
from app.models.password_reset import PasswordResetToken
from app.models.smart_table import WorkspaceItem, WorkspaceItemPermission
from app.services.workspace import (
    create_workspace_db,
    rename_workspace_db,
    delete_workspace_db,
    create_folder,
    create_folder_db,
    create_table,
    create_table_db,
    create_dashboard,
    create_dashboard_db,
    rename_item,
    rename_item_db,
    delete_item,
    delete_item_db,
    move_item,
    move_item_db,
    copy_table,
    copy_table_db,
    copy_dashboard,
    copy_dashboard_db,
)
from graphql import GraphQLError


async def _stage_invitation_reset_token(db: AsyncSession, user_id: int) -> str:
    """Stage one password-setup credential without committing the invitation."""
    await db.execute(
        delete(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
    )
    raw_token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user_id,
            token_hash=_hash_reset_token(raw_token),
            expires_at=datetime.utcnow()
            + timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES),
        )
    )
    return raw_token


@strawberry.type
class WorkspaceMutations:
    """Mixin for workspace-related mutation resolvers"""
    
    @strawberry.mutation
    async def create_folder(
        self, info: Info, name: str, parent_id: str, workspace_id: Optional[str] = None
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, _ = await _resolve_workspace_for_user(
                info, workspace_id
            )
            await _require_item_permission(info, parent_id, "edit")
            return await create_folder_db(
                db, name, parent_id, resolved_workspace_id
            )
        return create_folder(name, parent_id, workspace_id)

    @strawberry.mutation
    async def create_table(
        self, info: Info, name: str, parent_id: str, workspace_id: Optional[str] = None,
        template_id: Optional[str] = None,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            user, resolved_workspace_id, _ = await _resolve_workspace_for_user(
                info, workspace_id
            )
            await _require_item_permission(info, parent_id, "edit")
            return await create_table_db(
                db,
                name,
                parent_id,
                "v1",
                resolved_workspace_id,
                template_id,
                user.id,
            )
        return create_table(name, parent_id, "v1", workspace_id, template_id)

    @strawberry.mutation
    async def create_dashboard(
        self,
        info: Info,
        name: str,
        parent_id: str,
        workspace_id: Optional[str] = None,
    ) -> JSON:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, _ = await _resolve_workspace_for_user(
                info, workspace_id
            )
            await _require_item_permission(info, parent_id, "edit")
            return await create_dashboard_db(db, name, parent_id, resolved_workspace_id)
        return create_dashboard(name, parent_id, workspace_id)

    @strawberry.mutation
    async def rename_item(
        self, info: Info, item_id: str, name: str, workspace_id: Optional[str] = None
    ) -> bool:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, _ = await _resolve_workspace_for_user(
                info, workspace_id
            )
            await _require_item_permission(info, item_id, "manage")
            return await rename_item_db(db, item_id, name, resolved_workspace_id)
        return rename_item(item_id, name, workspace_id)

    @strawberry.mutation
    async def delete_item(
        self, info: Info, item_id: str, workspace_id: Optional[str] = None
    ) -> bool:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, _ = await _resolve_workspace_for_user(
                info, workspace_id
            )
            await _require_item_permission(info, item_id, "manage")
            return await delete_item_db(db, item_id, resolved_workspace_id)
        return delete_item(item_id, workspace_id)

    @strawberry.mutation
    async def move_item(
        self,
        info: Info,
        item_id: str,
        new_parent_id: str,
        workspace_id: Optional[str] = None,
    ) -> bool:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, _ = await _resolve_workspace_for_user(
                info, workspace_id
            )
            await _require_item_permission(info, item_id, "manage")
            await _require_item_permission(info, new_parent_id, "edit")
            return await move_item_db(
                db, item_id, new_parent_id, resolved_workspace_id
            )
        return move_item(item_id, new_parent_id, workspace_id)

    @strawberry.mutation
    async def copy_table(
        self,
        info: Info,
        table_id: str,
        new_parent_id: str,
        new_name: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, _ = await _resolve_workspace_for_user(
                info, workspace_id
            )
            await _require_item_permission(info, table_id, "read")
            await _require_item_permission(info, new_parent_id, "edit")
            return await copy_table_db(
                db, table_id, new_parent_id, new_name, resolved_workspace_id
            )
        return copy_table(table_id, new_parent_id, new_name, workspace_id)

    @strawberry.mutation
    async def copy_dashboard(
        self,
        info: Info,
        dashboard_id: str,
        new_parent_id: str,
        new_name: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, _ = await _resolve_workspace_for_user(
                info, workspace_id
            )
            await _require_item_permission(info, dashboard_id, "read")
            await _require_item_permission(info, new_parent_id, "edit")
            return await copy_dashboard_db(
                db, dashboard_id, new_parent_id, new_name, resolved_workspace_id
            )
        return copy_dashboard(dashboard_id, new_parent_id, new_name, workspace_id)

    @strawberry.mutation(name="setItemPermission")
    async def setItemPermission(
        self,
        info: Info,
        item_id: str,
        user_id: int,
        permission: str,
        workspace_id: Optional[str] = None,
    ) -> bool:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) != "db":
            raise ValueError("Workspace not supported in file backend")
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
        if not result_item.scalars().first():
            raise ValueError("Item not found")
        await _require_item_permission(info, item_id, "manage")
        if normalize_permission(permission) != permission:
            raise ValueError("Invalid permission")
        result_member = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.workspace_id == resolved_workspace_id,
            )
        )
        if not result_member.scalars().first():
            raise ValueError("User not in workspace")
        result_existing = await db.execute(
            select(WorkspaceItemPermission).where(
                WorkspaceItemPermission.item_id == item_id,
                WorkspaceItemPermission.user_id == user_id,
            )
        )
        existing = result_existing.scalars().first()
        if existing:
            existing.permission = permission
        else:
            db.add(
                WorkspaceItemPermission(
                    item_id=item_id,
                    user_id=user_id,
                    permission=permission,
                )
            )
        await db.commit()
        return True

    @strawberry.mutation(name="removeItemPermission")
    async def removeItemPermission(
        self,
        info: Info,
        item_id: str,
        user_id: int,
        workspace_id: Optional[str] = None,
    ) -> bool:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) != "db":
            raise ValueError("Workspace not supported in file backend")
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
        if not result_item.scalars().first():
            raise ValueError("Item not found")
        await _require_item_permission(info, item_id, "manage")
        await db.execute(
            delete(WorkspaceItemPermission).where(
                WorkspaceItemPermission.item_id == item_id,
                WorkspaceItemPermission.user_id == user_id,
            )
        )
        await db.commit()
        return True

    @strawberry.mutation
    async def create_workspace(self, info: Info, name: str) -> JSON:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            user = await _require_user(info)
            payload = await create_workspace_db(db, name)
            workspace_id = payload.get("id")
            if workspace_id:
                result = await db.execute(
                    select(WorkspaceMember).where(
                        WorkspaceMember.user_id == user.id,
                        WorkspaceMember.workspace_id == workspace_id,
                    )
                )
                member = result.scalars().first()
                if not member:
                    member = WorkspaceMember(
                        user_id=user.id,
                        workspace_id=workspace_id,
                        role=WorkspaceRole.owner,
                    )
                    db.add(member)
                    await db.commit()
            return payload
        raise ValueError("Workspace not supported in file backend")

    @strawberry.mutation
    async def rename_workspace(self, info: Info, workspace_id: str, name: str) -> bool:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, member = await _resolve_workspace_for_user(
                info, workspace_id
            )
            if member.role != WorkspaceRole.owner:
                raise ValueError("Only owner can rename workspace")
            return await rename_workspace_db(db, resolved_workspace_id, name)
        return False

    @strawberry.mutation
    async def delete_workspace(self, info: Info, workspace_id: str) -> bool:
        db: AsyncSession = info.context["db"]
        if _resolve_backend(None) == "db":
            _, resolved_workspace_id, member = await _resolve_workspace_for_user(
                info, workspace_id
            )
            if member.role != WorkspaceRole.owner:
                raise ValueError("Only owner can delete workspace")
            return await delete_workspace_db(db, resolved_workspace_id)
        return False

    @strawberry.mutation(name="inviteUserToWorkspace")
    async def inviteUserToWorkspace(
        self, info: Info, email: str, workspace_id: strawberry.ID, role: str
    ) -> WorkspaceMemberInfo:
        if _resolve_backend(None) != "db":
            raise ValueError("Workspace not supported in file backend")
        db: AsyncSession = info.context["db"]
        inviter = await _require_user(info)
        workspace_id_value = str(workspace_id)
        await _require_workspace_exists(db, workspace_id_value)
        inviter_member = await _require_workspace_member(db, inviter.id, workspace_id_value)
        if inviter_member.role != WorkspaceRole.owner:
            raise ValueError("Only owner can invite")
        try:
            role_enum = WorkspaceRole(role)
        except ValueError:
            raise ValueError("Invalid role")

        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        reset_token: Optional[str] = None
        if not user:
            # New-user invitations need an actual password-setup delivery path.
            # Never create a dormant account/token that an inviter must ferry as
            # a raw credential through GraphQL.
            if not _smtp_configured():
                raise GraphQLError("Invitation email delivery is not configured")
            password = secrets.token_urlsafe(32)
            user = User(
                email=email,
                name=email.split("@")[0],
                password_hash=hash_password(password),
            )
            db.add(user)
            await db.flush()
            reset_token = await _stage_invitation_reset_token(db, user.id)

        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == user.id,
                WorkspaceMember.workspace_id == workspace_id_value,
            )
        )
        member = result.scalars().first()
        if member:
            member.role = role_enum
        else:
            member = WorkspaceMember(
                user_id=user.id,
                workspace_id=workspace_id_value,
                role=role_enum,
            )
            db.add(member)

        if reset_token:
            delivered = await run_in_threadpool(_send_reset_email, user.email, reset_token)
            if not delivered:
                # User, membership and reset-token hash are still uncommitted, so
                # one rollback removes the entire failed invitation atomically.
                await db.rollback()
                raise GraphQLError("Invitation email delivery failed")

        await db.commit()
        return WorkspaceMemberInfo(
            user_id=user.id,
            name=user.name,
            email=user.email,
            role=member.role.value if hasattr(member.role, "value") else str(member.role),
        )

    @strawberry.mutation(name="leaveWorkspace")
    async def leaveWorkspace(self, info: Info, workspace_id: strawberry.ID) -> bool:
        if _resolve_backend(None) != "db":
            raise ValueError("Workspace not supported in file backend")
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        workspace_id_value = str(workspace_id)
        await _require_workspace_exists(db, workspace_id_value)
        member = await _require_workspace_member(db, user.id, workspace_id_value)
        if member.role == WorkspaceRole.owner:
            result = await db.execute(
                select(func.count()).where(
                    WorkspaceMember.workspace_id == workspace_id_value,
                    WorkspaceMember.role == WorkspaceRole.owner,
                )
            )
            owner_count = result.scalar_one()
            if owner_count <= 1:
                raise ValueError("Cannot leave last owner")
        await db.delete(member)
        await db.commit()
        return True
