from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_workflow.service import (
    WorkflowRunRequest,
    WorkflowResumeRequest,
    WorkflowStatusResponse,
    agent_workflow_service,
)
from app.context_engine import ContextBuildInput, context_builder
from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.services.ai_tool_router import tool_router_service

router = APIRouter(prefix="/api/agent-workflow", tags=["agent-workflow"])
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
async def run_agent_workflow(
    payload: WorkflowRunRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def event_stream():
        workspace_id = payload.workspace_id or request.headers.get("X-Workspace-Id")
        normalized_payload = WorkflowRunRequest.model_validate(
            {
                **payload.model_dump(mode="json", by_alias=True),
                "sessionId": payload.session_id or request.headers.get("X-Session-Id"),
                "conversationId": payload.conversation_id or request.headers.get("X-Conversation-Id"),
                "workspaceId": workspace_id,
                "projectId": payload.project_id or request.headers.get("X-Project-Id"),
                "viewId": payload.view_id or request.headers.get("X-View-Id"),
                "taskId": payload.task_id or request.headers.get("X-Task-Id"),
                "teamId": payload.team_id or request.headers.get("X-Team-Id"),
                "organizationId": payload.organization_id or request.headers.get("X-Organization-Id"),
                "workflowDefId": payload.workflow_def_id or request.headers.get("X-Workflow-Def-Id"),
                "agentId": payload.agent_id or request.headers.get("X-Agent-Id"),
            }
        )

        try:
            ai_config = await tool_router_service.load_user_ai_config(db, user_id)
            model, provider = await tool_router_service.build_provider_model(
                ai_config,
                model_override=payload.model,
            )
        except Exception as exc:
            logger.exception("Failed to build AI model for workflow user_id=%s", user_id)
            yield _sse_event({
                "type": "error",
                "workflowId": None,
                "data": {"code": "MODEL_SETUP_FAILED", "message": str(exc)},
            })
            return

        build_input = ContextBuildInput(
            source="api.agent_workflow",
            userId=user_id,
            sessionId=normalized_payload.session_id,
            conversationId=normalized_payload.conversation_id,
            workspaceId=workspace_id,
            projectId=normalized_payload.project_id,
            tableIds=normalized_payload.table_ids,
            viewId=normalized_payload.view_id,
            taskId=normalized_payload.task_id,
            teamId=normalized_payload.team_id,
            organizationId=normalized_payload.organization_id,
            workflowId=normalized_payload.workflow_def_id,
            agentId=normalized_payload.agent_id,
            message=normalized_payload.message,
            locale=normalized_payload.locale,
            timezone=normalized_payload.timezone,
        )

        try:
            agent_context = await context_builder.build(
                db=db,
                build_input=build_input,
                trace_id=None,
            )
        except Exception as exc:
            logger.exception("Context engine failed for workflow user_id=%s", user_id)
            agent_context = None

        try:
            async for event in agent_workflow_service.run_workflow(
                db=db,
                user_id=user_id,
                request=normalized_payload,
                model=model,
                agent_context=agent_context,
            ):
                yield _sse_event(event.model_dump(mode="json", by_alias=True))
        except UsageLimitExceeded as exc:
            yield _sse_event({
                "type": "error",
                "workflowId": None,
                "data": {"code": "USAGE_LIMIT_EXCEEDED", "message": str(exc)},
            })
        except ModelHTTPError as exc:
            status_code = 429 if exc.status_code == 429 else 502
            yield _sse_event({
                "type": "error",
                "workflowId": None,
                "data": {
                    "code": "MODEL_PROVIDER_ERROR",
                    "message": str(exc),
                    "providerStatus": exc.status_code,
                    "model": getattr(exc, "model_name", None),
                },
            })
        except ModelAPIError as exc:
            yield _sse_event({
                "type": "error",
                "workflowId": None,
                "data": {
                    "code": "MODEL_API_ERROR",
                    "message": str(exc),
                    "model": getattr(exc, "model_name", None),
                },
            })
        except UnexpectedModelBehavior as exc:
            yield _sse_event({
                "type": "error",
                "workflowId": None,
                "data": {"code": "UNEXPECTED_MODEL_BEHAVIOR", "message": str(exc)},
            })
        except Exception as exc:
            logger.exception("Agent workflow streaming failed user_id=%s", user_id)
            yield _sse_event({
                "type": "error",
                "workflowId": None,
                "data": {"code": "WORKFLOW_FAILED", "message": str(exc)},
            })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/resume")
async def resume_agent_workflow(
    payload: WorkflowResumeRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    workflow_id = payload.workflow_id

    async def event_stream():
        try:
            ai_config = await tool_router_service.load_user_ai_config(db, user_id)
            model, provider = await tool_router_service.build_provider_model(ai_config)
        except Exception as exc:
            logger.exception("Failed to build AI model for resume user_id=%s", user_id)
            yield _sse_event({
                "type": "error",
                "workflowId": workflow_id,
                "data": {"code": "MODEL_SETUP_FAILED", "message": str(exc)},
            })
            return

        try:
            async for event in agent_workflow_service.resume_workflow(
                db=db,
                user_id=user_id,
                workflow_id=workflow_id,
                approval_id=payload.approval_id,
                approved=payload.approved,
                model=model,
            ):
                yield _sse_event(event.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            logger.exception("Agent workflow resume failed workflow_id=%s", workflow_id)
            yield _sse_event({
                "type": "error",
                "workflowId": workflow_id,
                "data": {"code": "RESUME_FAILED", "message": str(exc)},
            })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status/{workflow_id}", response_model=WorkflowStatusResponse)
async def get_workflow_status(
    workflow_id: str,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    status = await agent_workflow_service.get_workflow_status(db, workflow_id)
    if not status:
        raise HTTPException(status_code=404, detail="Workflow not found")

    return status


@router.get("/session/{session_id}")
async def get_session_workflows(
    session_id: str,
    user_id: int | None = Depends(get_current_user_id),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        from app.agent_workflow.persistence import redis_session_manager
        await redis_session_manager.connect()
        workflow_ids = await redis_session_manager.get_session_workflows(session_id)
        return {"sessionId": session_id, "workflowIds": workflow_ids}
    except Exception as exc:
        logger.warning("Failed to get session workflows: %s", exc)
        return {"sessionId": session_id, "workflowIds": []}
