from __future__ import annotations

from typing import Any

from app.context_engine.models import AgentContext, ContextBuildInput
from app.context_engine.builder import context_builder
from app.services.ai_tool_router import ToolContextState, ToolRouterRequest


class ContextInjector:
    async def build_and_inject(
        self,
        *,
        db,
        request: ToolRouterRequest,
        build_input: ContextBuildInput,
        trace_id: str | None = None,
    ) -> tuple[ToolRouterRequest, AgentContext]:
        agent_context = await context_builder.build(db, build_input, trace_id=trace_id)
        injected = self.inject(request=request, agent_context=agent_context)
        return injected, agent_context

    def inject(
        self,
        *,
        request: ToolRouterRequest,
        agent_context: AgentContext,
    ) -> ToolRouterRequest:
        current_tool_context = request.tool_context.model_dump(mode="json", by_alias=True)
        current_notes = list(request.tool_context.notes)
        current_notes.extend(
            [
                "以下字段由 Context Engine 自动注入，优先据此理解业务上下文。",
                agent_context.summary.text,
            ]
        )
        current_variables = dict(request.tool_context.variables)
        current_variables.update(
            {
                "_agent_context_scope": agent_context.scope_key,
                "_agent_context_highlights": agent_context.summary.highlights,
                "_agent_context_window": agent_context.window.model_dump(mode="json", by_alias=True),
            }
        )

        tool_context = ToolContextState.model_validate(
            {
                **current_tool_context,
                "sessionId": agent_context.session.id,
                "workspaceId": agent_context.workspace.id or request.workspace_id,
                "tableIds": agent_context.table.metadata.get("tableIds") or request.table_ids,
                "conversationId": agent_context.conversation.id,
                "projectId": agent_context.project.id,
                "viewId": agent_context.view.id,
                "taskId": agent_context.task.id,
                "teamId": agent_context.team.id,
                "organizationId": agent_context.organization.id,
                "workflow": agent_context.workflow.model_dump(mode="json", by_alias=True),
                "agentStack": [item.model_dump(mode="json", by_alias=True) for item in agent_context.agents],
                "contextSummary": agent_context.summary.model_dump(mode="json", by_alias=True),
                "contextSnapshot": agent_context.model_dump(mode="json", by_alias=True),
                "variables": current_variables,
                "notes": current_notes[-12:],
            }
        )

        payload: dict[str, Any] = request.model_dump(mode="json", by_alias=True)
        payload.update(
            {
                "workspaceId": agent_context.workspace.id or request.workspace_id,
                "tableIds": tool_context.table_ids or request.table_ids,
                "conversationId": agent_context.conversation.id or request.conversation_id,
                "sessionId": agent_context.session.id or request.session_id,
                "projectId": agent_context.project.id or request.project_id,
                "viewId": agent_context.view.id or request.view_id,
                "taskId": agent_context.task.id or request.task_id,
                "teamId": agent_context.team.id or request.team_id,
                "organizationId": agent_context.organization.id or request.organization_id,
                "workflowId": agent_context.workflow.id or request.workflow_id,
                "agentId": request.agent_id or (agent_context.agents[0].id if agent_context.agents else None),
                "toolContext": tool_context.model_dump(mode="json", by_alias=True),
            }
        )
        return ToolRouterRequest.model_validate(payload)


context_injector = ContextInjector()
