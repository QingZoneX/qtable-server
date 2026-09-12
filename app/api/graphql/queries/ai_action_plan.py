from __future__ import annotations

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.services.ai_action_plan import ai_action_plan_service


@strawberry.type
class AiActionPlanQueries:
    @strawberry.field(name="aiActionPlan")
    async def ai_action_plan(
        self,
        info: Info,
        plan_id: str,
    ) -> JSON | None:
        user = await _require_user(info)
        return await ai_action_plan_service.get_plan(
            info.context["db"],
            user_id=user.id,
            plan_id=plan_id,
        )
