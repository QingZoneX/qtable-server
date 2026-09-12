from __future__ import annotations

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.services.ai_visual_design import ai_visual_design_service


@strawberry.type
class AiVisualDesignQueries:
    @strawberry.field(name="aiVisualDesignPlan")
    async def ai_visual_design_plan(
        self,
        info: Info,
        plan_id: str,
    ) -> JSON | None:
        user = await _require_user(info)
        return await ai_visual_design_service.get_plan(
            info.context["db"],
            user_id=user.id,
            plan_id=plan_id,
        )
