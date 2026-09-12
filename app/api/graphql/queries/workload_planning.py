from __future__ import annotations

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.services.workload_planning import workload_planning_service


@strawberry.type
class WorkloadPlanningQueries:
    @strawberry.field(name="workloadPlanningBatch")
    async def workload_planning_batch(
        self,
        info: Info,
        batch_id: str,
    ) -> JSON | None:
        user = await _require_user(info)
        return await workload_planning_service.get_batch(
            info.context["db"],
            user_id=user.id,
            batch_id=batch_id,
        )
