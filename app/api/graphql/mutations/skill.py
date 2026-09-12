from __future__ import annotations

from typing import Any, cast

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.api.graphql.queries.skill import _to_skill_entry
from app.api.graphql.types import (
    SkillCallErrorInfo,
    SkillCallInput,
    SkillCallResultInfo,
    SkillRegistryEntryInfo,
    SkillRegistryInput,
    SkillStatusInput,
)
from app.services.skill_registry import (
    SkillRegistrationRequest,
    SkillStatusUpdateRequest,
    skill_registry_service,
)
from app.skills.contracts import SkillCallRequest, SkillContext
from app.skills.runtime import SkillExecutionContext, SkillRuntime


@strawberry.type
class SkillMutations:
    @strawberry.mutation(name="registerSkill")
    async def register_skill(
        self,
        info: Info,
        input: SkillRegistryInput,
    ) -> SkillRegistryEntryInfo:
        user = await _require_user(info)
        db = info.context["db"]
        payload = SkillRegistrationRequest.model_validate(
            {
                "name": input.name,
                "version": input.version,
                "title": input.title,
                "description": input.description,
                "category": input.category,
                "namespace": input.namespace,
                "workspaceId": input.workspaceId,
                "tags": input.tags,
                "visibility": input.visibility,
                "source_type": input.sourceType,
                "runtime_kind": input.runtimeKind,
                "side_effect": input.sideEffect,
                "confirmation_required": input.confirmationRequired,
                "idempotent": input.idempotent,
                "supports_dry_run": input.supportsDryRun,
                "entrypoint": input.entrypoint,
                "modulePath": input.modulePath,
                "handlerName": input.handlerName,
                "icon": input.icon,
                "inputSchema": input.inputSchema,
                "outputSchema": input.outputSchema,
                "permissions": input.permissions,
                "transportConfig": input.transportConfig,
                "openaiToolSchema": input.openaiToolSchema,
                "mcpToolSchema": input.mcpToolSchema,
                "cacheTtlSeconds": input.cacheTtlSeconds,
                "embeddingText": input.embeddingText,
                "changelog": input.changelog,
            }
        )
        entry = await skill_registry_service.register_skill(db, payload, actor=str(user.id))
        return _to_skill_entry(entry)

    @strawberry.mutation(name="setSkillStatus")
    async def set_skill_status(
        self,
        info: Info,
        skillId: str,
        input: SkillStatusInput,
    ) -> SkillRegistryEntryInfo:
        await _require_user(info)
        db = info.context["db"]
        updated = await skill_registry_service.set_skill_status(
            db,
            skill_id=skillId,
            payload=SkillStatusUpdateRequest.model_validate(
                {"isEnabled": input.isEnabled, "status": input.status}
            ),
        )
        if updated is None:
            raise GraphQLError("Skill not found")
        return _to_skill_entry(updated)

    @strawberry.mutation(name="callSkill")
    async def call_skill(
        self,
        info: Info,
        input: SkillCallInput,
    ) -> SkillCallResultInfo:
        user = await _require_user(info)
        db = info.context["db"]
        runtime = SkillRuntime(
            await skill_registry_service.build_runtime_registry(db, workspace_id=None)
        )
        request = SkillCallRequest.model_validate(
            {
                "skill_name": input.skillName,
                "input": cast(dict[str, Any], input.input),
                "dry_run": input.dryRun,
                "confirmed": input.confirmed,
                "origin": input.origin,
                "trace_id": input.traceId,
            }
        )
        response = await runtime.invoke(
            request,
            SkillExecutionContext(
                context=SkillContext(
                    user_id=user.id,
                    workspace_id=(info.context.get("request").headers.get("X-Workspace-Id") if info.context.get("request") else None),
                    session_id=(info.context.get("request").headers.get("X-Session-Id") if info.context.get("request") else None),
                    conversation_id=(info.context.get("request").headers.get("X-Conversation-Id") if info.context.get("request") else None),
                    project_id=(info.context.get("request").headers.get("X-Project-Id") if info.context.get("request") else None),
                    view_id=(info.context.get("request").headers.get("X-View-Id") if info.context.get("request") else None),
                    task_id=(info.context.get("request").headers.get("X-Task-Id") if info.context.get("request") else None),
                    team_id=(info.context.get("request").headers.get("X-Team-Id") if info.context.get("request") else None),
                    organization_id=(info.context.get("request").headers.get("X-Organization-Id") if info.context.get("request") else None),
                    workflow_id=(info.context.get("request").headers.get("X-Workflow-Id") if info.context.get("request") else None),
                    agent_id=(info.context.get("request").headers.get("X-Agent-Id") if info.context.get("request") else None),
                    origin="graphql",
                    trace_id=input.traceId,
                    dry_run=input.dryRun,
                    confirmed=input.confirmed,
                ),
                db=db,
            ),
        )
        error = None
        if response.error:
            error = SkillCallErrorInfo(
                code=response.error.code.value
                if hasattr(response.error.code, "value")
                else str(response.error.code),
                message=response.error.message,
                retryable=response.error.retryable,
                details=response.error.details,
            )
        return SkillCallResultInfo(
            callId=response.call_id,
            skillName=response.skill_name,
            state=response.state.value if hasattr(response.state, "value") else str(response.state),
            output=response.output,
            error=error,
            requiresConfirmation=response.requires_confirmation,
            metadata=response.metadata,
        )
