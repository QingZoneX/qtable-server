from __future__ import annotations

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.services.project_steward import project_steward_service


@strawberry.type
class ProjectStewardQueries:
    @strawberry.field(name="projectStewardDiagnosis")
    async def project_steward_diagnosis(
        self,
        info: Info,
        diagnosis_id: str,
    ) -> JSON | None:
        user = await _require_user(info)
        return await project_steward_service.get_diagnosis(
            info.context["db"],
            user_id=user.id,
            diagnosis_id=diagnosis_id,
        )
