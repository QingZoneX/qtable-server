from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.schemas.project_steward import ProjectStewardRequest
from app.services.project_steward import ProjectStewardError, project_steward_service


@strawberry.type
class ProjectStewardMutations:
    @strawberry.mutation(name="projectStewardAsk")
    async def project_steward_ask(
        self,
        info: Info,
        workspace_id: str,
        table_ids: list[str],
        question: str,
        project_id: Optional[str] = None,
        timezone: str = "Asia/Shanghai",
        due_soon_days: int = 3,
        stale_task_days: int = 7,
        overload_hours: float = 40.0,
        overload_window_days: int = 7,
        max_records: int = 500,
        model: Optional[str] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Project steward requires database backend")
        user = await _require_user(info)
        try:
            request = ProjectStewardRequest.model_validate(
                {
                    "workspaceId": workspace_id,
                    "tableIds": table_ids,
                    "question": question,
                    "projectId": project_id,
                    "timezone": timezone,
                    "dueSoonDays": due_soon_days,
                    "staleTaskDays": stale_task_days,
                    "overloadHours": overload_hours,
                    "overloadWindowDays": overload_window_days,
                    "maxRecords": max_records,
                    "model": model,
                }
            )
            return await project_steward_service.ask(
                info.context["db"],
                user_id=user.id,
                request=request,
            )
        except (ProjectStewardError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
