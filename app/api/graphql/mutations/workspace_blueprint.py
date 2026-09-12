from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import (
    _require_item_permission,
    _resolve_backend,
    _resolve_workspace_for_user,
)
from app.models.smart_table import WorkspaceItem
from app.services.workspace_blueprint import (
    WorkspaceBlueprintError,
    apply_workspace_blueprint,
    preview_workspace_blueprint,
)


@strawberry.type
class WorkspaceBlueprintMutations:
    @strawberry.mutation(name="previewGoalWorkspace")
    async def preview_goal_workspace(
        self,
        info: Info,
        goal: str,
        workspace_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        team_size: Optional[int] = None,
        deadline: Optional[str] = None,
        current_blueprint: Optional[JSON] = None,
        instruction: Optional[str] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Goal-driven workspace requires database backend")

        db: AsyncSession = info.context["db"]
        user, resolved_workspace_id, _ = await _resolve_workspace_for_user(
            info, workspace_id
        )

        resolved_parent_id = parent_id
        if not resolved_parent_id:
            result = await db.execute(
                select(WorkspaceItem.id).where(
                    WorkspaceItem.workspace_id == resolved_workspace_id,
                    WorkspaceItem.parent_id.is_(None),
                )
            )
            resolved_parent_id = result.scalars().first()
        if not resolved_parent_id:
            raise GraphQLError("Workspace root not found")

        await _require_item_permission(info, resolved_parent_id, "edit")
        try:
            return await preview_workspace_blueprint(
                db,
                user_id=user.id,
                workspace_id=resolved_workspace_id,
                parent_id=resolved_parent_id,
                goal=goal,
                team_size=team_size,
                deadline=deadline,
                current_blueprint=(
                    dict(current_blueprint)
                    if isinstance(current_blueprint, dict)
                    else None
                ),
                instruction=instruction,
            )
        except WorkspaceBlueprintError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="applyGoalWorkspaceBlueprint")
    async def apply_goal_workspace_blueprint(
        self,
        info: Info,
        trace_id: str,
        blueprint: JSON,
        workspace_id: str,
        parent_id: str,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Goal-driven workspace requires database backend")
        if not isinstance(blueprint, dict):
            raise GraphQLError("Blueprint must be an object")

        db: AsyncSession = info.context["db"]
        user, resolved_workspace_id, _ = await _resolve_workspace_for_user(
            info, workspace_id
        )
        if resolved_workspace_id != workspace_id:
            raise GraphQLError("Workspace mismatch")
        await _require_item_permission(info, parent_id, "edit")

        try:
            return await apply_workspace_blueprint(
                db,
                trace_id=trace_id,
                user_id=user.id,
                workspace_id=resolved_workspace_id,
                parent_id=parent_id,
                blueprint=dict(blueprint),
            )
        except WorkspaceBlueprintError as exc:
            raise GraphQLError(str(exc)) from exc
