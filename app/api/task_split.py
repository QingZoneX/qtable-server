from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.models.task_split import TaskSplitPlan
from app.schemas.task_split import TaskSplitRequest, TaskSplitResponse
from app.services.task_split import task_split_service

router = APIRouter(prefix="/api/task-split", tags=["task-split"])


async def get_current_user_id(request: Request) -> int | None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    try:
        payload = decode_access_token(token)
        user_id = payload.get("user_id")
        return int(user_id) if user_id is not None else None
    except Exception:
        return None


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


@router.post("/run", response_model=TaskSplitResponse)
async def run_task_split(
    payload: TaskSplitRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    normalized = TaskSplitRequest.model_validate(
        {
            **payload.model_dump(mode="json", by_alias=True),
            "workspaceId": payload.workspace_id or request.headers.get("X-Workspace-Id"),
            "sessionId": payload.session_id or request.headers.get("X-Session-Id"),
            "conversationId": payload.conversation_id or request.headers.get("X-Conversation-Id"),
            "projectId": payload.project_id or request.headers.get("X-Project-Id"),
            "viewId": payload.view_id or request.headers.get("X-View-Id"),
            "taskId": payload.task_id or request.headers.get("X-Task-Id"),
            "teamId": payload.team_id or request.headers.get("X-Team-Id"),
            "organizationId": payload.organization_id or request.headers.get("X-Organization-Id"),
            "workflowId": payload.workflow_id or request.headers.get("X-Workflow-Id"),
            "agentId": payload.agent_id or request.headers.get("X-Agent-Id"),
        }
    )
    response = await task_split_service.run(
        db=db,
        user_id=user_id,
        request=normalized,
    )
    if response.error:
        raise HTTPException(status_code=500, detail=response.error)
    return response


@router.get("/plans/{plan_id}")
async def get_task_split_plan(
    plan_id: str,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = await db.execute(
        select(TaskSplitPlan)
        .where(
            TaskSplitPlan.id == plan_id,
            TaskSplitPlan.created_by == str(user_id),
        )
        .limit(1)
    )
    plan = result.scalars().first()
    if not plan:
        raise HTTPException(status_code=404, detail="Task split plan not found")
    return {
        "id": plan.id,
        "workspaceId": plan.workspace_id,
        "projectId": plan.project_id,
        "taskId": plan.task_id,
        "prompt": plan.source_prompt,
        "goal": plan.normalized_goal,
        "summary": plan.summary,
        "mermaid": plan.mermaid,
        "provider": plan.provider,
        "model": plan.model,
        "traceId": plan.trace_id,
        "result": plan.structured_output,
        "createdAt": plan.created_at.isoformat() if plan.created_at else None,
        "updatedAt": plan.updated_at.isoformat() if plan.updated_at else None,
    }
