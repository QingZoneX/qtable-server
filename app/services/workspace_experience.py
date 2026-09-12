from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole


EXPERIENCE_MODES = {"simple", "advanced"}
DEFAULT_EXPERIENCE_MODE = "simple"


class WorkspaceExperienceError(ValueError):
    """Raised when an experience preference cannot be read or changed safely."""


def normalize_experience_mode(value: Optional[str], *, allow_none: bool = False) -> Optional[str]:
    if value is None and allow_none:
        return None
    mode = str(value or "").strip().lower()
    if mode not in EXPERIENCE_MODES:
        raise WorkspaceExperienceError("Experience mode must be 'simple' or 'advanced'")
    return mode


async def _workspace_and_member(
    db: AsyncSession,
    *,
    user_id: int,
    workspace_id: str,
) -> tuple[Workspace, WorkspaceMember]:
    result = await db.execute(
        select(Workspace, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(
            Workspace.id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    row = result.first()
    if row is None:
        raise PermissionError("Workspace not found or no access")
    return row[0], row[1]


def _serialize(workspace: Workspace, member: WorkspaceMember) -> dict[str, Any]:
    workspace_default = (
        normalize_experience_mode(workspace.experience_mode)
        if workspace.experience_mode
        else DEFAULT_EXPERIENCE_MODE
    )
    user_override = (
        normalize_experience_mode(member.experience_mode, allow_none=True)
        if member.experience_mode is not None
        else None
    )
    return {
        "workspaceId": workspace.id,
        "workspaceDefaultMode": workspace_default,
        "userMode": user_override,
        "effectiveMode": user_override or workspace_default,
        "followsWorkspaceDefault": user_override is None,
        "canManageWorkspaceDefault": member.role == WorkspaceRole.owner,
    }


async def get_workspace_experience(
    db: AsyncSession,
    *,
    user_id: int,
    workspace_id: str,
) -> dict[str, Any]:
    workspace, member = await _workspace_and_member(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    return _serialize(workspace, member)


async def set_user_experience_mode(
    db: AsyncSession,
    *,
    user_id: int,
    workspace_id: str,
    mode: Optional[str],
) -> dict[str, Any]:
    workspace, member = await _workspace_and_member(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    member.experience_mode = normalize_experience_mode(mode, allow_none=True)
    await db.commit()
    await db.refresh(member)
    return _serialize(workspace, member)


async def set_workspace_default_experience_mode(
    db: AsyncSession,
    *,
    user_id: int,
    workspace_id: str,
    mode: str,
) -> dict[str, Any]:
    workspace, member = await _workspace_and_member(
        db,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    if member.role != WorkspaceRole.owner:
        raise PermissionError("Only workspace owner can change the default experience mode")
    workspace.experience_mode = normalize_experience_mode(mode) or DEFAULT_EXPERIENCE_MODE
    await db.commit()
    await db.refresh(workspace)
    return _serialize(workspace, member)
