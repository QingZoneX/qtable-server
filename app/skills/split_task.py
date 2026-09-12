from __future__ import annotations

from pydantic import Field

from app.schemas.task_split import TaskSplitRequest, TaskSplitResponse
from app.services.task_split import task_split_service
from app.skills.contracts import SkillErrorCode


class SplitTaskSkillInput(TaskSplitRequest):
    prompt: str = Field(min_length=1, description="The high-level project or delivery goal to split.")


async def handle_split_task(
    context,
    data: SplitTaskSkillInput,
) -> dict:
    from app.skills.runtime import SkillRuntimeError

    if context.context.user_id is None or context.db is None:
        raise SkillRuntimeError(
            SkillErrorCode.UNAUTHORIZED,
            "Split task skill requires an authenticated user and database session",
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
    response = await task_split_service.run(
        db=context.db,
        user_id=int(context.context.user_id),
        request=request,
    )
    if response.error:
        raise SkillRuntimeError(
            SkillErrorCode.EXECUTION_FAILED,
            response.error.get("message") or "Task split failed",
            retryable=True,
            details=response.error,
        )
    return response.model_dump(mode="json", by_alias=True)


def build_split_task_skill_definition():
    from app.skills.runtime import SkillDefinition, SkillMetadata, SkillSideEffect

    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.task.split",
            title="Split Complex Task",
            description="Split a complex project goal into a task tree with dependencies, estimates, risks, Mermaid, and persisted task records. Invoke when planning delivery or decomposing large work.",
            tags=["task", "planning", "project", "agent", "workflow"],
            side_effect=SkillSideEffect.WRITE,
            confirmation_required=True,
            idempotent=False,
            supports_dry_run=True,
            permissions=[],
        ),
        input_model=SplitTaskSkillInput,
        output_model=TaskSplitResponse,
        handler=handle_split_task,
    )
