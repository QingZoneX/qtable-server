from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from sqlalchemy.exc import DBAPIError, OperationalError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.schemas.workload_planning import (
    WorkloadPlanningApplyRequest,
    WorkloadPlanningFeedbackRequest,
    WorkloadPlanningPreviewRequest,
    WorkloadPlanningWhatIfRequest,
)
from app.services.workload_planning import (
    WorkloadPlanningError,
    workload_planning_service,
)


@strawberry.type
class WorkloadPlanningMutations:
    @strawberry.mutation(name="previewWorkloadPlanning")
    async def preview_workload_planning(
        self,
        info: Info,
        workspace_id: str,
        table_id: str,
        record_ids: Optional[list[str]] = None,
        deadline: Optional[str] = None,
        team_size: int = 3,
        parallel_streams: Optional[int] = None,
        business_domain: Optional[str] = None,
        quality_bar: str = "production",
        source_reference: Optional[JSON] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Workload planning requires database backend")
        user = await _require_user(info)
        try:
            payload = WorkloadPlanningPreviewRequest.model_validate(
                {
                    "workspaceId": workspace_id,
                    "tableId": table_id,
                    "recordIds": record_ids or [],
                    "deadline": deadline,
                    "teamSize": team_size,
                    "parallelStreams": parallel_streams,
                    "businessDomain": business_domain,
                    "qualityBar": quality_bar,
                    "sourceReference": (
                        dict(source_reference)
                        if isinstance(source_reference, dict)
                        else None
                    ),
                }
            )
            return await workload_planning_service.preview(
                info.context["db"],
                user_id=user.id,
                request=payload,
            )
        except (WorkloadPlanningError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
        except (DBAPIError, OperationalError) as exc:
            if "recovery mode" in str(exc).lower():
                raise GraphQLError(
                    "数据库正在恢复，暂时无法执行工作量预估；请稍后重试。"
                ) from exc
            raise

    @strawberry.mutation(name="applyWorkloadPlanning")
    async def apply_workload_planning(
        self,
        info: Info,
        batch_id: str,
        workspace_id: str,
        table_id: str,
        record_ids: Optional[list[str]] = None,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Workload planning requires database backend")
        user = await _require_user(info)
        try:
            return await workload_planning_service.apply(
                info.context["db"],
                user_id=user.id,
                request=WorkloadPlanningApplyRequest(
                    batchId=batch_id,
                    workspaceId=workspace_id,
                    tableId=table_id,
                    recordIds=record_ids or [],
                ),
            )
        except (WorkloadPlanningError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="workloadPlanningWhatIf")
    async def workload_planning_what_if(
        self,
        info: Info,
        batch_id: str,
        team_size: Optional[int] = None,
        parallel_streams: Optional[int] = None,
        deadline: Optional[str] = None,
    ) -> JSON:
        user = await _require_user(info)
        try:
            return await workload_planning_service.what_if(
                info.context["db"],
                user_id=user.id,
                request=WorkloadPlanningWhatIfRequest(
                    batchId=batch_id,
                    teamSize=team_size,
                    parallelStreams=parallel_streams,
                    deadline=deadline,
                ),
            )
        except (WorkloadPlanningError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="submitWorkloadPlanningFeedback")
    async def submit_workload_planning_feedback(
        self,
        info: Info,
        batch_id: str,
        record_id: str,
        actual_story_points: Optional[float] = None,
        actual_hours: Optional[float] = None,
        outcome_status: str = "on_track",
        accuracy_rating: Optional[int] = None,
        notes: str = "",
    ) -> JSON:
        user = await _require_user(info)
        try:
            return await workload_planning_service.submit_feedback(
                info.context["db"],
                user_id=user.id,
                request=WorkloadPlanningFeedbackRequest(
                    batchId=batch_id,
                    recordId=record_id,
                    actualStoryPoints=actual_story_points,
                    actualHours=actual_hours,
                    outcomeStatus=outcome_status,
                    accuracyRating=accuracy_rating,
                    notes=notes,
                ),
            )
        except (WorkloadPlanningError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
