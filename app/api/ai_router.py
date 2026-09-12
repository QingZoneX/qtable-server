from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
    UserError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine import ContextBuildInput, context_injector
from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.services.ai_tool_router import (
    ToolRouterRequest,
    ToolRouterResponse,
    tool_router_service,
)

router = APIRouter(prefix="/api/ai/router", tags=["ai-router"])
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


@router.post("/run", response_model=ToolRouterResponse)
async def run_tool_router(
    payload: ToolRouterRequest,
    request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    workspace_id = (
        payload.workspace_id
        or payload.tool_context.workspace_id
        or request.headers.get("X-Workspace-Id")
    )
    normalized_payload = ToolRouterRequest.model_validate(
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
            "workflowId": payload.workflow_id or request.headers.get("X-Workflow-Id"),
            "agentId": payload.agent_id or request.headers.get("X-Agent-Id"),
        }
    )

    try:
        logger.info(
            "api.ai_router.run start user_id=%s workspace_id=%s tables=%s",
            user_id,
            workspace_id,
            normalized_payload.table_ids,
        )
        injected_request, agent_context = await context_injector.build_and_inject(
            db=db,
            request=normalized_payload,
            build_input=ContextBuildInput(
                source="api.ai_router",
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
                workflowId=normalized_payload.workflow_id,
                agentId=normalized_payload.agent_id,
                message=normalized_payload.message,
                locale=normalized_payload.locale,
                timezone=normalized_payload.timezone,
            ),
        )
        response = await tool_router_service.run(
            db=db,
            user_id=user_id,
            request=injected_request,
            agent_context=agent_context,
        )
        logger.info(
            "api.ai_router.run finish user_id=%s trace_id=%s status=%s",
            user_id,
            response.trace_id,
            response.status,
        )
        return response
    except ValueError as exc:
        logger.warning(
            "api.ai_router.run invalid_request user_id=%s workspace_id=%s message=%s",
            user_id,
            workspace_id,
            str(exc),
        )
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        ) from exc
    except UsageLimitExceeded as exc:
        logger.warning("api.ai_router.run usage_limit user_id=%s message=%s", user_id, str(exc))
        raise HTTPException(
            status_code=429,
            detail={"code": "USAGE_LIMIT_EXCEEDED", "message": str(exc)},
        ) from exc
    except ModelHTTPError as exc:
        status_code = 429 if exc.status_code == 429 else 502
        logger.warning(
            "api.ai_router.run model_http_error user_id=%s status_code=%s model=%s",
            user_id,
            exc.status_code,
            getattr(exc, "model_name", None),
        )
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": "MODEL_PROVIDER_ERROR",
                "message": str(exc),
                "providerStatus": exc.status_code,
                "model": getattr(exc, "model_name", None),
            },
        ) from exc
    except ModelAPIError as exc:
        logger.warning(
            "api.ai_router.run model_api_error user_id=%s model=%s message=%s",
            user_id,
            getattr(exc, "model_name", None),
            str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "MODEL_API_ERROR",
                "message": str(exc),
                "model": getattr(exc, "model_name", None),
            },
        ) from exc
    except UnexpectedModelBehavior as exc:
        logger.warning("api.ai_router.run unexpected_model_behavior user_id=%s", user_id)
        raise HTTPException(
            status_code=502,
            detail={"code": "UNEXPECTED_MODEL_BEHAVIOR", "message": str(exc)},
        ) from exc
    except UserError as exc:
        logger.warning("api.ai_router.run user_error user_id=%s message=%s", user_id, str(exc))
        raise HTTPException(
            status_code=400,
            detail={"code": "AGENT_CONFIGURATION_ERROR", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception(
            "api.ai_router.run failed user_id=%s workspace_id=%s",
            user_id,
            workspace_id,
        )
        raise HTTPException(
            status_code=500,
            detail={"code": "TOOL_ROUTER_FAILED", "message": str(exc) or "Tool router failed"},
        ) from exc
