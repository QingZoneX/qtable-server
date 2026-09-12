from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.api.graphql.types import TaskSplitResultInfo, TaskSplitRunInput
from app.schemas.task_planning import (
    TaskPlanningApplyRequest,
    TaskPlanningPreviewRequest,
)
from app.schemas.task_split import TaskSplitPlan, TaskSplitRequest
from app.services.task_duplicates import TaskDuplicateError, task_duplicate_service
from app.services.task_planning import TaskPlanningError, task_planning_service
from app.services.task_split import task_split_service


@strawberry.type
class TaskSplitMutations:
    @strawberry.mutation(name="splitTaskPlan")
    async def split_task_plan(
        self,
        info: Info,
        input: TaskSplitRunInput,
    ) -> TaskSplitResultInfo:
        user = await _require_user(info)
        db = info.context["db"]
        request = info.context.get("request")
        payload = TaskSplitRequest.model_validate(
            {
                "prompt": input.prompt,
                "model": input.model,
                "autoCreateRecords": input.autoCreateRecords,
                "dryRun": input.dryRun,
                "maxDepth": input.maxDepth,
                "maxChildrenPerNode": input.maxChildrenPerNode,
                "workspaceId": input.workspaceId or (request.headers.get("X-Workspace-Id") if request else None),
                "sessionId": input.sessionId or (request.headers.get("X-Session-Id") if request else None),
                "conversationId": input.conversationId or (request.headers.get("X-Conversation-Id") if request else None),
                "projectId": input.projectId or (request.headers.get("X-Project-Id") if request else None),
                "tableIds": list(input.tableIds),
                "viewId": input.viewId or (request.headers.get("X-View-Id") if request else None),
                "taskId": input.taskId or (request.headers.get("X-Task-Id") if request else None),
                "teamId": input.teamId or (request.headers.get("X-Team-Id") if request else None),
                "organizationId": input.organizationId or (request.headers.get("X-Organization-Id") if request else None),
                "workflowId": input.workflowId or (request.headers.get("X-Workflow-Id") if request else None),
                "agentId": input.agentId or (request.headers.get("X-Agent-Id") if request else None),
                "locale": input.locale,
                "timezone": input.timezone,
                "retry": {
                    "maxAttempts": input.retry.maxAttempts,
                    "maxOutputRetries": input.retry.maxOutputRetries,
                    "maxToolRetries": input.retry.maxToolRetries,
                    "backoffMs": input.retry.backoffMs,
                },
            }
        )
        response = await task_split_service.run(
            db=db,
            user_id=user.id,
            request=payload,
        )
        return TaskSplitResultInfo(
            traceId=response.trace_id,
            provider=response.provider,
            model=response.model,
            attempts=response.attempts,
            persisted=response.persisted,
            planId=response.plan_id,
            result=response.result.model_dump(mode="json", by_alias=True),
            error=response.error,
            createdAt=response.created_at.isoformat() if response.created_at else None,
        )


    @strawberry.mutation(name="previewTaskPlanning")
    async def preview_task_planning(
        self,
        info: Info,
        goal: str,
        workspace_id: str,
        target_table_id: str,
        project_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        selected_record_ids: Optional[list[str]] = None,
        source_reference: Optional[JSON] = None,
        deadline: Optional[str] = None,
        team_size: Optional[int] = None,
        granularity: str = "balanced",
        max_depth: int = 3,
        max_children_per_node: int = 8,
        locale: str = "zh-CN",
        timezone: str = "Asia/Shanghai",
    ) -> JSON:
        user = await _require_user(info)
        db = info.context["db"]
        try:
            payload = TaskPlanningPreviewRequest.model_validate(
                {
                    "goal": goal,
                    "workspaceId": workspace_id,
                    "targetTableId": target_table_id,
                    "projectId": project_id,
                    "parentTaskId": parent_task_id,
                    "selectedRecordIds": selected_record_ids or [],
                    "sourceReference": (
                        dict(source_reference)
                        if isinstance(source_reference, dict)
                        else None
                    ),
                    "deadline": deadline,
                    "teamSize": team_size,
                    "granularity": granularity,
                    "maxDepth": max_depth,
                    "maxChildrenPerNode": max_children_per_node,
                    "locale": locale,
                    "timezone": timezone,
                }
            )
            result = await task_planning_service.preview(
                db,
                user_id=user.id,
                request=payload,
            )
            try:
                plan = result.get("plan") if isinstance(result, dict) else None
                field_mapping = result.get("fieldMapping") if isinstance(result, dict) else None
                if isinstance(plan, dict):
                    result["duplicates"] = await task_duplicate_service.scan_plan(
                        db,
                        user_id=user.id,
                        table_id=target_table_id,
                        plan=plan,
                        field_mapping=(
                            field_mapping if isinstance(field_mapping, dict) else None
                        ),
                    )
                    result["duplicateThresholds"] = task_duplicate_service.thresholds()
                    result["duplicatePermissionScope"] = {
                        "rowVisibilityApplied": True,
                        "hiddenRecordsExcluded": True,
                        "advisoryOnly": True,
                    }
            except TaskDuplicateError as exc:
                warnings = list(result.get("warnings") or [])
                warnings.append(f"重复任务检测不可用：{exc}")
                result["warnings"] = warnings
            return result
        except (TaskPlanningError, TaskDuplicateError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="applyTaskPlanning")
    async def apply_task_planning(
        self,
        info: Info,
        plan_id: str,
        workspace_id: str,
        target_table_id: str,
        plan: JSON,
        decisions: Optional[list[JSON]] = None,
        allow_repeat: bool = False,
    ) -> JSON:
        user = await _require_user(info)
        db = info.context["db"]
        if not isinstance(plan, dict):
            raise GraphQLError("Task plan must be an object")
        try:
            payload = TaskPlanningApplyRequest.model_validate(
                {
                    "planId": plan_id,
                    "workspaceId": workspace_id,
                    "targetTableId": target_table_id,
                    "plan": TaskSplitPlan.model_validate(plan),
                    "decisions": [
                        dict(item)
                        for item in (decisions or [])
                        if isinstance(item, dict)
                    ],
                    "allowRepeat": allow_repeat,
                }
            )
            return await task_planning_service.apply(
                db,
                user_id=user.id,
                request=payload,
            )
        except (TaskPlanningError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
