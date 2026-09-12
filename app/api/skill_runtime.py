from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.skills.contracts import SkillCallRequest, SkillContext
from app.skills.runtime import SkillExecutionContext, SkillRuntime
from app.services.skill_registry import (
    SkillRegistrationRequest,
    SkillStatusUpdateRequest,
    skill_registry_service,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


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


@router.get("/manifest")
async def get_skill_manifest(
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    workspace_id: str | None = Query(default=None, alias="workspaceId"),
    search: str | None = Query(default=None),
    category: str | None = Query(default=None),
    include_disabled: bool = Query(default=False, alias="includeDisabled"),
):
    if not user_id:
        return {"skills": [], "error": {"code": "UNAUTHORIZED", "message": "Unauthorized"}}
    skills = await skill_registry_service.list_manifest(
        db,
        workspace_id=workspace_id,
        search=search,
        category_slug=category,
        include_disabled=include_disabled,
    )
    categories = await skill_registry_service.list_categories(db)
    return {
        "skills": skills,
        "categories": categories,
        "registry": {
            "workspaceId": workspace_id,
            "search": search,
            "category": category,
            "includeDisabled": include_disabled,
            "cacheLayer": "redis+postgres",
        },
    }


@router.get("/categories")
async def get_skill_categories(
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        return {"categories": [], "error": {"code": "UNAUTHORIZED", "message": "Unauthorized"}}
    return {"categories": await skill_registry_service.list_categories(db)}


@router.post("/registry/register")
async def register_skill(
    request: SkillRegistrationRequest,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    registered = await skill_registry_service.register_skill(
        db,
        request,
        actor=str(user_id),
    )
    return {"skill": registered}


@router.post("/registry/{skill_id}/status")
async def update_skill_status(
    skill_id: str,
    request: SkillStatusUpdateRequest,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    updated = await skill_registry_service.set_skill_status(
        db,
        skill_id=skill_id,
        payload=request,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"skill": updated}


@router.post("/call")
async def call_skill(
    request: SkillCallRequest,
    raw_request: Request,
    user_id: int | None = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    workspace_id = raw_request.headers.get("X-Workspace-Id")
    runtime = SkillRuntime(
        await skill_registry_service.build_runtime_registry(db, workspace_id=workspace_id)
    )
    context = SkillExecutionContext(
        context=SkillContext(
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=raw_request.headers.get("X-Session-Id"),
            conversation_id=raw_request.headers.get("X-Conversation-Id"),
            project_id=raw_request.headers.get("X-Project-Id"),
            table_ids=request.input.get("tableIds", []) if isinstance(request.input, dict) else [],
            view_id=raw_request.headers.get("X-View-Id"),
            task_id=raw_request.headers.get("X-Task-Id"),
            team_id=raw_request.headers.get("X-Team-Id"),
            organization_id=raw_request.headers.get("X-Organization-Id"),
            workflow_id=raw_request.headers.get("X-Workflow-Id"),
            agent_id=raw_request.headers.get("X-Agent-Id"),
            request_id=raw_request.headers.get("X-Request-Id"),
            trace_id=request.trace_id,
            origin=request.origin,
            dry_run=request.dry_run,
            confirmed=request.confirmed,
        ),
        db=db,
    )
    response = await runtime.invoke(request, context)
    return response.model_dump(mode="json", by_alias=True)
