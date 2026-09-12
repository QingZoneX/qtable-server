from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic_ai.exceptions import (
    ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_workflow.pm_agent.service import PMAgentRunRequest, pm_agent_service
from app.schemas.pm_agent import PMAgentRunRequest as SchemaRunRequest, PMAgentStatusResponse
from app.context_engine import ContextBuildInput, context_builder
from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.services.ai_tool_router import tool_router_service

router = APIRouter(prefix="/api/pm-agent", tags=["pm-agent"])
logger = logging.getLogger(__name__)


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


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@router.post("/run")
async def run_pm_agent(
    payload: SchemaRunRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def event_stream():
        workspace_id = payload.workspace_id or request.headers.get("X-Workspace-Id")
        svc_request = PMAgentRunRequest(
            message=payload.message,
            model=payload.model,
            sessionId=payload.session_id or request.headers.get("X-Session-Id"),
            conversationId=payload.conversation_id or request.headers.get("X-Conversation-Id"),
            workspaceId=workspace_id,
            projectId=payload.project_id or request.headers.get("X-Project-Id"),
            tableIds=payload.table_ids or [],
            viewId=payload.view_id or request.headers.get("X-View-Id"),
            taskId=payload.task_id or request.headers.get("X-Task-Id"),
            teamId=payload.team_id or request.headers.get("X-Team-Id"),
            organizationId=payload.organization_id or request.headers.get("X-Organization-Id"),
            workflowDefId=payload.workflow_def_id or request.headers.get("X-Workflow-Def-Id"),
            agentId=payload.agent_id or request.headers.get("X-Agent-Id"),
            locale=payload.locale,
            timezone=payload.timezone,
            streamingEnabled=payload.streaming_enabled,
        )

        try:
            ai_config = await tool_router_service.load_user_ai_config(db, user_id)
            model, provider = await tool_router_service.build_provider_model(
                ai_config, model_override=payload.model,
            )
        except Exception as exc:
            logger.exception("Failed to build AI model for PM Agent user_id=%s", user_id)
            yield _sse_event({"type": "error", "workflowId": None,
                              "data": {"code": "MODEL_SETUP_FAILED", "message": str(exc)}})
            return

        build_input = ContextBuildInput(
            source="api.pm_agent", userId=user_id,
            sessionId=svc_request.session_id, conversationId=svc_request.conversation_id,
            workspaceId=svc_request.workspace_id, projectId=svc_request.project_id,
            tableIds=svc_request.table_ids, viewId=svc_request.view_id,
            taskId=svc_request.task_id, teamId=svc_request.team_id,
            organizationId=svc_request.organization_id,
            locale=svc_request.locale, timezone=svc_request.timezone,
        )
        agent_context = await context_builder.build(build_input, db=db)

        try:
            async for event in pm_agent_service.run(
                db=db, user_id=user_id, request=svc_request,
                model=model, agent_context=agent_context,
            ):
                yield _sse_event(event.model_dump(mode="json", by_alias=True))
        except (ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded) as exc:
            logger.exception("AI model error during PM Agent")
            yield _sse_event({"type": "error", "workflowId": None,
                              "data": {"code": "AI_MODEL_ERROR", "message": str(exc)}})
        except Exception as exc:
            logger.exception("PM Agent execution failed")
            yield _sse_event({"type": "error", "workflowId": None,
                              "data": {"code": "PM_AGENT_ERROR", "message": str(exc)}})

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/status/{workflow_id}")
async def get_pm_agent_status(
    workflow_id: str, request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    status = await pm_agent_service.get_status(db, workflow_id, user_id)
    if not status:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return JSONResponse(content=status.model_dump(mode="json", by_alias=True))


@router.get("/history")
async def list_pm_agent_history(
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    offset: int = 0, limit: int = 20,
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    from app.agent_workflow.persistence import redis_session_manager as rsm
    try:
        workflows = await rsm.get_session_workflows(str(user_id))
    except Exception:
        workflows = []
    return JSONResponse(content={
        "workflowIds": workflows[offset:offset + limit],
        "total": len(workflows), "offset": offset, "limit": limit,
    })
