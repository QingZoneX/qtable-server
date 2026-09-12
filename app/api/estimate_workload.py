from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.models.estimate_workload import WorkloadEstimateRun
from app.schemas.estimate_workload import (
    EstimateFeedbackRequest,
    EstimateWorkloadRequest,
    EstimateWorkloadResponse,
)
from app.services.estimate_workload import estimate_workload_service

router = APIRouter(prefix="/api/estimate-workload", tags=["estimate-workload"])


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


@router.post("/run", response_model=EstimateWorkloadResponse)
async def run_estimate_workload(
    payload: EstimateWorkloadRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    normalized = EstimateWorkloadRequest.model_validate(
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
    response = await estimate_workload_service.run(
        db=db,
        user_id=user_id,
        request=normalized,
    )
    if response.error:
        raise HTTPException(status_code=500, detail=response.error)
    return response


@router.get("/estimates/{estimate_id}")
async def get_estimate_workload(
    estimate_id: str,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = await db.execute(
        select(WorkloadEstimateRun)
        .where(
            WorkloadEstimateRun.id == estimate_id,
            WorkloadEstimateRun.created_by == str(user_id),
        )
        .limit(1)
    )
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="Estimate not found")
    return {
        "id": row.id,
        "workspaceId": row.workspace_id,
        "projectId": row.project_id,
        "taskId": row.task_id,
        "teamId": row.team_id,
        "prompt": row.source_prompt,
        "scope": row.normalized_scope,
        "businessDomain": row.business_domain,
        "qualityBar": row.quality_bar,
        "traceId": row.trace_id,
        "provider": row.provider,
        "model": row.model,
        "status": row.status,
        "confidenceScore": row.confidence_score,
        "result": row.structured_output,
        "historicalLearning": row.historical_summary,
        "actualStoryPoints": row.actual_story_points,
        "actualHours": row.actual_hours,
        "outcomeStatus": row.outcome_status,
        "accuracyRating": row.accuracy_rating,
        "feedbackNotes": row.feedback_notes,
        "feedbackAt": row.feedback_at.isoformat() if row.feedback_at else None,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/estimates/{estimate_id}/feedback")
async def submit_estimate_feedback(
    estimate_id: str,
    payload: EstimateFeedbackRequest,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    updated = await estimate_workload_service.submit_feedback(
        db,
        estimate_id=estimate_id,
        user_id=user_id,
        feedback=payload,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Estimate not found")
    return {
        "id": updated.id,
        "status": updated.status,
        "actualStoryPoints": updated.actual_story_points,
        "actualHours": updated.actual_hours,
        "outcomeStatus": updated.outcome_status,
        "accuracyRating": updated.accuracy_rating,
        "feedbackNotes": updated.feedback_notes,
        "feedbackAt": updated.feedback_at.isoformat() if updated.feedback_at else None,
    }
