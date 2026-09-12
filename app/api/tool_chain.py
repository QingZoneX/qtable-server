from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.schemas.tool_chain import ToolChainRequest, ToolChainRunResponse
from app.services.tool_chain import tool_chain_runtime_service

router = APIRouter(prefix="/api/tool-chains", tags=["tool-chains"])
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


def _normalize_request(payload: ToolChainRequest, request: Request, stream: bool = False) -> ToolChainRequest:
    return ToolChainRequest.model_validate(
        {
            **payload.model_dump(mode="json", by_alias=True),
            "sessionId": payload.session_id or request.headers.get("X-Session-Id"),
            "conversationId": payload.conversation_id or request.headers.get("X-Conversation-Id"),
            "workspaceId": payload.workspace_id or request.headers.get("X-Workspace-Id"),
            "projectId": payload.project_id or request.headers.get("X-Project-Id"),
            "viewId": payload.view_id or request.headers.get("X-View-Id"),
            "taskId": payload.task_id or request.headers.get("X-Task-Id"),
            "teamId": payload.team_id or request.headers.get("X-Team-Id"),
            "organizationId": payload.organization_id or request.headers.get("X-Organization-Id"),
            "workflowId": payload.workflow_id or request.headers.get("X-Workflow-Id"),
            "agentId": payload.agent_id or request.headers.get("X-Agent-Id"),
            "stream": stream,
        }
    )


@router.post("/run", response_model=ToolChainRunResponse)
async def run_tool_chain(
    payload: ToolChainRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    normalized = _normalize_request(payload, request, stream=False)
    try:
        return await tool_chain_runtime_service.run(
            db=db,
            user_id=user_id,
            request=normalized,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_TOOL_CHAIN_REQUEST", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception("tool_chain run failed user_id=%s", user_id)
        raise HTTPException(
            status_code=500,
            detail={"code": "TOOL_CHAIN_FAILED", "message": str(exc) or "Tool chain failed"},
        ) from exc


@router.post("/stream")
async def stream_tool_chain(
    payload: ToolChainRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    normalized = _normalize_request(payload, request, stream=True)

    async def generate():
        try:
            async for event in tool_chain_runtime_service.stream(
                db=db,
                user_id=user_id,
                request=normalized,
            ):
                yield _sse_event(event.model_dump(mode="json", by_alias=True))
        except Exception as exc:
            logger.exception("tool_chain stream failed user_id=%s", user_id)
            yield _sse_event(
                {
                    "type": "run_failed",
                    "runId": "unknown",
                    "traceId": "unknown",
                    "data": {
                        "code": "TOOL_CHAIN_STREAM_FAILED",
                        "message": str(exc) or "Tool chain stream failed",
                    },
                }
            )

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/runs/{run_id}", response_model=ToolChainRunResponse)
async def get_tool_chain_run(
    run_id: str,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    response = await tool_chain_runtime_service.get_run(db=db, run_id=run_id)
    if response is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TOOL_CHAIN_RUN_NOT_FOUND", "message": "Tool chain run not found"},
        )
    return response
