from __future__ import annotations

from app.schemas.estimate_workload import EstimateWorkloadRequest, EstimateWorkloadResponse
from app.services.estimate_workload import estimate_workload_service
from app.skills.contracts import SkillErrorCode


class EstimateWorkloadSkillInput(EstimateWorkloadRequest):
    pass


async def handle_estimate_workload(
    context,
    data: EstimateWorkloadSkillInput,
) -> dict:
    from app.skills.runtime import SkillRuntimeError

    if context.context.user_id is None or context.db is None:
        raise SkillRuntimeError(
            SkillErrorCode.UNAUTHORIZED,
            "Estimate workload skill requires an authenticated user and database session",
        )
    request = data.model_copy(
        update={
            "workspace_id": data.workspace_id or context.context.workspace_id,
            "session_id": data.session_id or context.context.session_id,
            "conversation_id": data.conversation_id or context.context.conversation_id,
            "project_id": data.project_id or context.context.project_id,
            "table_ids": data.table_ids or context.context.table_ids,
            "view_id": data.view_id or context.context.view_id,
            "task_id": data.task_id or context.context.task_id,
            "team_id": data.team_id or context.context.team_id,
            "organization_id": data.organization_id or context.context.organization_id,
            "workflow_id": data.workflow_id or context.context.workflow_id,
            "agent_id": data.agent_id or context.context.agent_id,
            "locale": data.locale or context.context.locale,
            "timezone": data.timezone or context.context.timezone,
            "dry_run": bool(context.context.dry_run or data.dry_run),
        }
    )
    response = await estimate_workload_service.run(
        db=context.db,
        user_id=int(context.context.user_id),
        request=request,
    )
    if response.error:
        raise SkillRuntimeError(
            SkillErrorCode.EXECUTION_FAILED,
            response.error.get("message") or "Estimate workload failed",
            retryable=True,
            details=response.error,
        )
    return response.model_dump(mode="json", by_alias=True)


def build_estimate_workload_skill_definition():
    from app.skills.runtime import SkillDefinition, SkillMetadata, SkillSideEffect

    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.project.estimate_workload",
            title="Estimate Project Workload",
            description="Estimate complex project workload with story points, P50/P90, risk factors, team capability, tech stack impact, historical learning, confidence, and context-aware structured output. Invoke when sizing delivery scope or planning execution.",
            tags=["estimate", "planning", "project", "agent", "delivery"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            permissions=[],
        ),
        input_model=EstimateWorkloadSkillInput,
        output_model=EstimateWorkloadResponse,
        handler=handle_estimate_workload,
    )
