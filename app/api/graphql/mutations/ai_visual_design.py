from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend, publish_table_update
from app.schemas.ai_visual_design import (
    AiVisualDesignApplyRequest,
    AiVisualDesignPreviewRequest,
)
from app.services.ai_visual_design import (
    AiVisualDesignError,
    ai_visual_design_service,
)


@strawberry.type
class AiVisualDesignMutations:
    @strawberry.mutation(name="previewAiVisualDesign")
    async def preview_ai_visual_design(
        self,
        info: Info,
        workspace_id: str,
        prompt: str,
        target_type: str = "auto",
        table_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        dashboard_id: Optional[str] = None,
        current_proposal: Optional[JSON] = None,
        instruction: Optional[str] = None,
        model: Optional[str] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("AI visual design requires database backend")
        user = await _require_user(info)
        try:
            request = AiVisualDesignPreviewRequest.model_validate(
                {
                    "workspaceId": workspace_id,
                    "prompt": prompt,
                    "targetType": target_type,
                    "tableId": table_id,
                    "parentId": parent_id,
                    "dashboardId": dashboard_id,
                    "currentProposal": current_proposal,
                    "instruction": instruction,
                    "model": model,
                }
            )
            return await ai_visual_design_service.preview(
                info.context["db"],
                user_id=user.id,
                request=request,
            )
        except (AiVisualDesignError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="applyAiVisualDesign")
    async def apply_ai_visual_design(
        self,
        info: Info,
        plan_id: str,
        proposal: Optional[JSON] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("AI visual design requires database backend")
        user = await _require_user(info)
        try:
            result = await ai_visual_design_service.apply(
                info.context["db"],
                user_id=user.id,
                request=AiVisualDesignApplyRequest.model_validate(
                    {
                        "planId": plan_id,
                        "proposal": proposal,
                    }
                ),
            )
        except (AiVisualDesignError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

        view = result.get("view")
        if isinstance(view, dict) and view.get("tableId"):
            await publish_table_update(info.context["db"], str(view["tableId"]))
        return result
