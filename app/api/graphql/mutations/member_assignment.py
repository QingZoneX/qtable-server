from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.schemas.member_assignment import (
    MemberAssignmentApplyRequest,
    MemberAssignmentPreviewRequest,
    MemberAssignmentWhatIfRequest,
)
from app.services.member_assignment import MemberAssignmentError, member_assignment_service


@strawberry.type
class MemberAssignmentMutations:
    @strawberry.mutation(name="previewMemberAssignment")
    async def preview_member_assignment(
        self,
        info: Info,
        workspace_id: str,
        table_id: str,
        record_ids: Optional[list[str]] = None,
        workload_batch_id: Optional[str] = None,
        member_profiles: Optional[list[JSON]] = None,
        capacity_overrides: Optional[JSON] = None,
        locked_assignments: Optional[JSON] = None,
        preserve_existing_assignees: bool = True,
        top_k: int = 3,
        default_task_hours: float = 8.0,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Member assignment requires database backend")
        user = await _require_user(info)
        try:
            request = MemberAssignmentPreviewRequest.model_validate(
                {
                    "workspaceId": workspace_id,
                    "tableId": table_id,
                    "recordIds": record_ids or [],
                    "workloadBatchId": workload_batch_id,
                    "memberProfiles": member_profiles or [],
                    "capacityOverrides": dict(capacity_overrides or {}),
                    "lockedAssignments": dict(locked_assignments or {}),
                    "preserveExistingAssignees": preserve_existing_assignees,
                    "topK": top_k,
                    "defaultTaskHours": default_task_hours,
                }
            )
            return await member_assignment_service.preview(
                info.context["db"],
                user_id=user.id,
                request=request,
            )
        except (MemberAssignmentError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="memberAssignmentWhatIf")
    async def member_assignment_what_if(
        self,
        info: Info,
        batch_id: str,
        capacity_overrides: Optional[JSON] = None,
    ) -> JSON:
        user = await _require_user(info)
        try:
            request = MemberAssignmentWhatIfRequest.model_validate(
                {
                    "batchId": batch_id,
                    "capacityOverrides": dict(capacity_overrides or {}),
                }
            )
            return await member_assignment_service.what_if(
                info.context["db"],
                user_id=user.id,
                request=request,
            )
        except (MemberAssignmentError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="applyMemberAssignment")
    async def apply_member_assignment(
        self,
        info: Info,
        batch_id: str,
        workspace_id: str,
        table_id: str,
        assignments: list[JSON],
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Member assignment requires database backend")
        user = await _require_user(info)
        try:
            request = MemberAssignmentApplyRequest.model_validate(
                {
                    "batchId": batch_id,
                    "workspaceId": workspace_id,
                    "tableId": table_id,
                    "assignments": assignments,
                }
            )
            return await member_assignment_service.apply(
                info.context["db"],
                user_id=user.id,
                request=request,
            )
        except (MemberAssignmentError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
