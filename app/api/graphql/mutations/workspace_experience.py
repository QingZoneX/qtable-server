from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.workspace_experience import (
    WorkspaceExperienceError,
    set_user_experience_mode,
    set_workspace_default_experience_mode,
)


@strawberry.type
class WorkspaceExperienceMutations:
    @strawberry.mutation(name="setMyWorkspaceExperienceMode")
    async def set_my_workspace_experience_mode(
        self,
        info: Info,
        workspace_id: strawberry.ID,
        mode: Optional[str] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Experience preferences require database backend")
        user = await _require_user(info)
        db = info.context["db"]
        try:
            payload = await set_user_experience_mode(
                db,
                user_id=user.id,
                workspace_id=str(workspace_id),
                mode=mode,
            )
            payload["persistent"] = True
            return payload
        except (WorkspaceExperienceError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="setWorkspaceDefaultExperienceMode")
    async def set_workspace_default_experience_mode_mutation(
        self,
        info: Info,
        workspace_id: strawberry.ID,
        mode: str,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Experience preferences require database backend")
        user = await _require_user(info)
        db = info.context["db"]
        try:
            payload = await set_workspace_default_experience_mode(
                db,
                user_id=user.id,
                workspace_id=str(workspace_id),
                mode=mode,
            )
            payload["persistent"] = True
            return payload
        except (WorkspaceExperienceError, PermissionError, ValueError) as exc:
            await db.rollback()
            raise GraphQLError(str(exc)) from exc
