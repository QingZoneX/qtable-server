from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend, publish_table_update
from app.schemas.ai_action_plan import (
    AiActionPlanApplyRequest,
    AiActionPlanPreviewRequest,
)
from app.services.ai_action_plan import AiActionPlanError, ai_action_plan_service


@strawberry.type
class AiActionPlanMutations:
    @strawberry.mutation(name="previewAiActionPlan")
    async def preview_ai_action_plan(
        self,
        info: Info,
        diagnosis_id: str,
        member_assignment_batch_id: Optional[str] = None,
        proposed_actions: Optional[list[JSON]] = None,
        model: Optional[str] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("AI Action Plan requires database backend")
        user = await _require_user(info)
        try:
            request = AiActionPlanPreviewRequest.model_validate(
                {
                    "diagnosisId": diagnosis_id,
                    "memberAssignmentBatchId": member_assignment_batch_id,
                    "proposedActions": proposed_actions,
                    "model": model,
                }
            )
            return await ai_action_plan_service.preview(
                info.context["db"],
                user_id=user.id,
                request=request,
            )
        except (AiActionPlanError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="applyAiActionPlan")
    async def apply_ai_action_plan(
        self,
        info: Info,
        plan_id: str,
        action_ids: Optional[list[str]] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("AI Action Plan requires database backend")
        user = await _require_user(info)
        try:
            result = await ai_action_plan_service.apply(
                info.context["db"],
                user_id=user.id,
                request=AiActionPlanApplyRequest.model_validate(
                    {
                        "planId": plan_id,
                        "actionIds": action_ids or [],
                    }
                ),
            )
        except (AiActionPlanError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

        for table_id in result.get("affectedTableIds") or []:
            await publish_table_update(info.context["db"], str(table_id))
        return result
