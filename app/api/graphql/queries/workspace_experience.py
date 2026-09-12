from __future__ import annotations

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.workspace_experience import (
    WorkspaceExperienceError,
    get_workspace_experience,
)


@strawberry.type
class WorkspaceExperienceQueries:
    @strawberry.field(name="workspaceExperiencePreference")
    async def workspace_experience_preference(
        self,
        info: Info,
        workspace_id: strawberry.ID,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            return {
                "workspaceId": str(workspace_id),
                "workspaceDefaultMode": "simple",
                "userMode": None,
                "effectiveMode": "simple",
                "followsWorkspaceDefault": True,
                "canManageWorkspaceDefault": False,
                "persistent": False,
            }
        user = await _require_user(info)
        try:
            payload = await get_workspace_experience(
                info.context["db"],
                user_id=user.id,
                workspace_id=str(workspace_id),
            )
            payload["persistent"] = True
            return payload
        except (WorkspaceExperienceError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
