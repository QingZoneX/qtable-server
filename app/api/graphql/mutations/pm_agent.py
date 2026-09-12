from typing import List, Optional, AsyncGenerator

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.types import (
    PMAgentStatusResponse,
    PMAgentRunResponse,
    PMAgentRunInput,
)
from app.agent_workflow.pm_agent.service import (
    pm_agent_service,
    PMAgentRunRequest,
    PMAgentEvent,
)
from app.context_engine import ContextBuildInput, context_builder
from app.services.ai_tool_router import tool_router_service

import logging

logger = logging.getLogger(__name__)


@strawberry.type
class PMAgentMutations:
    @strawberry.mutation
    async def pm_agent_run(
        self,
        info: Info,
        input: PMAgentRunInput,
    ) -> PMAgentRunResponse:
        db: AsyncSession = info.context["db"]
        user_id = info.context.get("user", {}).get("id")

        if not user_id:
            return PMAgentRunResponse(workflowId="", status="error", message="Unauthorized")

        try:
            ai_config = await tool_router_service.load_user_ai_config(db, user_id)
            model, provider = await tool_router_service.build_provider_model(ai_config, model_override=input.model)
        except Exception as exc:
            return PMAgentRunResponse(workflowId="", status="error", message=str(exc))

        build_input = ContextBuildInput(
            source="api.graphql.pm_agent",
            userId=user_id,
            sessionId=input.session_id,
            conversationId=input.conversation_id,
            workspaceId=input.workspace_id,
            projectId=input.project_id,
            tableIds=list(input.table_ids) if input.table_ids else [],
            viewId=input.view_id,
            taskId=input.task_id,
            teamId=input.team_id,
            organizationId=input.organization_id,
            locale=input.locale,
            timezone=input.timezone,
        )
        agent_context = await context_builder.build(build_input, db=db)

        request = PMAgentRunRequest(
            message=input.message,
            model=input.model,
            sessionId=input.session_id,
            conversationId=input.conversation_id,
            workspaceId=input.workspace_id,
            projectId=input.project_id,
            tableIds=list(input.table_ids) if input.table_ids else [],
            viewId=input.view_id,
            taskId=input.task_id,
            teamId=input.team_id,
            organizationId=input.organization_id,
            workflowDefId=input.workflow_def_id,
            agentId=input.agent_id,
            locale=input.locale,
            timezone=input.timezone,
            streamingEnabled=input.streaming_enabled,
            autoApproveReadonly=input.auto_approve_readonly,
        )

        first_event = None
        async for event in pm_agent_service.run(
            db=db,
            user_id=user_id,
            request=request,
            model=model,
            agent_context=agent_context,
        ):
            first_event = event
            break

        if first_event:
            return PMAgentRunResponse(
                workflowId=first_event.workflow_id,
                status=first_event.type,
                message="PM Agent workflow started",
            )

        return PMAgentRunResponse(workflowId="", status="error", message="Failed to start PM Agent")


@strawberry.type
class PMAgentSubscription:
    @strawberry.subscription(name="pmAgentStream")
    async def pm_agent_stream(
        self,
        info: Info,
        workflow_id: str,
    ) -> AsyncGenerator[JSON, None]:
        db: AsyncSession = info.context["db"]
        user_id = info.context.get("user", {}).get("id")

        if not user_id:
            yield {"type": "error", "message": "Unauthorized"}
            return

        from app.agent_workflow.persistence import redis_session_manager as rsm

        try:
            await rsm.connect()
            pubsub = await rsm.subscribe_workflow_events(workflow_id)
        except Exception as exc:
            logger.warning("Failed to subscribe to PM Agent events: %s", exc)
            yield {"type": "error", "message": str(exc)}
            return

        try:
            while True:
                message = await pubsub.get_message(timeout=30, ignore_subscribe_messages=True)
                if message and message.get("data"):
                    try:
                        import json
                        yield json.loads(message["data"])
                    except Exception:
                        yield {"type": "event", "data": message.get("data")}
        except Exception:
            pass
        finally:
            try:
                await pubsub.unsubscribe()
                await pubsub.close()
            except Exception:
                pass
