from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.schemas.structured_output import StructuredOutputRequest, StructuredOutputResponse
from app.services.structured_output import structured_output_service

router = APIRouter(prefix="/api/structured-output", tags=["structured-output"])
logger = logging.getLogger(__name__)


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


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


@router.post("/run", response_model=StructuredOutputResponse)
async def run_structured_output(
    payload: StructuredOutputRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        normalized_payload = StructuredOutputRequest.model_validate(
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
        return await structured_output_service.run(
            db=db,
            user_id=user_id,
            request=normalized_payload,
        )
    except ValueError as exc:
        logger.warning("structured_output invalid request user_id=%s error=%s", user_id, str(exc))
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_STRUCTURED_OUTPUT_REQUEST", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception("structured_output run failed user_id=%s", user_id)
        raise HTTPException(
            status_code=500,
            detail={"code": "STRUCTURED_OUTPUT_FAILED", "message": str(exc) or "Structured output failed"},
        ) from exc


@router.post("/stream")
async def stream_structured_output(
    payload: StructuredOutputRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    normalized_payload = StructuredOutputRequest.model_validate(
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
            "stream": True,
        }
    )

    async def generate():
        try:
            async for event in structured_output_service.stream(
                db=db,
                user_id=user_id,
                request=normalized_payload,
            ):
                yield _sse_event(event.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            logger.exception("structured_output stream failed user_id=%s", user_id)
            yield _sse_event(
                {
                    "type": "error",
                    "traceId": "unknown",
                    "data": {
                        "code": "STRUCTURED_OUTPUT_STREAM_FAILED",
                        "message": str(exc) or "Structured output stream failed",
                    },
                }
            )

    return StreamingResponse(generate(), media_type="text/event-stream")
