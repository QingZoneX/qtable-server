from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_workflow.pm_agent.state import PMAgentState, PMAgentMemory
from app.agent_workflow.pm_agent.graph import pm_agent_graph
from app.agent_workflow.persistence import workflow_persistence_manager, redis_session_manager
from app.agent_workflow.state import WorkflowConfig
from app.context_engine.models import AgentContext

logger = logging.getLogger(__name__)


class PMAgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    model: Optional[str] = None
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    conversation_id: Optional[str] = Field(default=None, alias="conversationId")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    view_id: Optional[str] = Field(default=None, alias="viewId")
    task_id: Optional[str] = Field(default=None, alias="taskId")
    team_id: Optional[str] = Field(default=None, alias="teamId")
    organization_id: Optional[str] = Field(default=None, alias="organizationId")
    workflow_def_id: Optional[str] = Field(default=None, alias="workflowDefId")
    agent_id: Optional[str] = Field(default=None, alias="agentId")
    config: Optional[WorkflowConfig] = None
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    streaming_enabled: bool = Field(default=True, alias="streamingEnabled")


class PMAgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    workflow_id: str = Field(alias="workflowId")
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class PMAgentStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(alias="workflowId")
    status: str
    pm_phase: Optional[str] = Field(default=None, alias="pmPhase")
    phase_sequence: list[str] = Field(default_factory=list, alias="phaseSequence")
    completed_phases: int = Field(default=0, alias="completedPhases")
    total_phases: int = Field(default=0, alias="totalPhases")
    phase_results: list[dict[str, Any]] = Field(default_factory=list, alias="phaseResults")
    final_response: Optional[str] = Field(default=None, alias="finalResponse")
    error: Optional[dict[str, Any]] = None
    created_at: Optional[str] = Field(default=None, alias="createdAt")
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")


class PMAgentService:
    async def run(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: PMAgentRunRequest,
        model,
        agent_context: Optional[AgentContext] = None,
    ) -> AsyncIterator[PMAgentEvent]:
        workflow_id = str(uuid.uuid4())
        session_id = request.session_id or workflow_id
        config = request.config or WorkflowConfig()

        state = PMAgentState(
            workflowId=workflow_id,
            sessionId=session_id,
            userId=user_id,
            conversationId=request.conversation_id,
            workspaceId=request.workspace_id,
            projectId=request.project_id,
            tableIds=request.table_ids,
            viewId=request.view_id,
            taskId=request.task_id,
            teamId=request.team_id,
            organizationId=request.organization_id,
            workflowDefId=request.workflow_def_id,
            agentId=request.agent_id,
            userMessage=request.message,
            locale=request.locale,
            timezone=request.timezone,
            config=config,
            streamingEnabled=request.streaming_enabled,
            agentMemory=PMAgentMemory(sessionId=session_id),
            metadata={
                "_db": db,
                "_model": model,
                "_provider": getattr(model, "model_name", "unknown"),
            },
            agentContext=agent_context.model_dump(mode="json", by_alias=True) if agent_context else None,
        )

        try:
            await redis_session_manager.connect()
        except Exception as exc:
            logger.warning("Redis connect failed, continuing: %s", exc)

        try:
            await workflow_persistence_manager.create_workflow(
                db, workflow_id=workflow_id, user_id=user_id,
                session_id=request.session_id, conversation_id=request.conversation_id,
                workspace_id=request.workspace_id, table_ids=request.table_ids,
                user_message=request.message,
                config_snapshot=config.model_dump(mode="json", by_alias=True),
                context_snapshot=agent_context.model_dump(mode="json", by_alias=True) if agent_context else {},
                retry_policy={"maxRetriesPerStep": config.max_retries_per_step},
                agent_context=agent_context.model_dump(mode="json", by_alias=True) if agent_context else None,
                project_id=request.project_id, view_id=request.view_id,
                task_id=request.task_id, team_id=request.team_id,
                organization_id=request.organization_id,
                workflow_def_id=request.workflow_def_id, agent_id=request.agent_id,
            )
        except Exception as exc:
            logger.exception("Failed to persist PM workflow: %s", exc)

        yield PMAgentEvent(
            type="pm_agent_started", workflowId=workflow_id,
            data={"message": request.message, "status": "initializing"},
        )

        compiled = pm_agent_graph.compiled
        langgraph_config = {
            "configurable": {"thread_id": workflow_id, "checkpoint_ns": "pm_agent"},
        }

        try:
            final_state = None
            async for event in compiled.astream(
                state.model_dump(mode="json", by_alias=True),
                config=langgraph_config,
                stream_mode="values",
            ):
                final_state = event if isinstance(event, dict) else event

                evt_data: dict[str, Any] = {"status": event.get("status", "executing") if isinstance(event, dict) else "executing"}
                if isinstance(event, dict):
                    if event.get("pmPhase"):
                        evt_data["pmPhase"] = event["pmPhase"]
                    if event.get("completedPhases") is not None:
                        evt_data["completedPhases"] = event["completedPhases"]
                        evt_data["totalPhases"] = event.get("totalPhases", 0)
                    if event.get("phaseResults"):
                        last_r = event["phaseResults"][-1] if event["phaseResults"] else None
                        if last_r:
                            evt_data["latestPhaseResult"] = last_r

                yield PMAgentEvent(
                    type="pm_phase_update", workflowId=workflow_id, data=evt_data,
                )

                try:
                    if request.streaming_enabled:
                        await redis_session_manager.publish_workflow_event(
                            workflow_id, {"type": "pm_phase_update", "workflowId": workflow_id, "data": evt_data},
                        )
                except Exception:
                    pass

            if final_state and isinstance(final_state, dict):
                state_from_graph = PMAgentState.model_validate(final_state)
            else:
                state_from_graph = state
                state_from_graph.status = "completed"

            if state_from_graph.status == "failed":
                yield PMAgentEvent(
                    type="pm_agent_failed", workflowId=workflow_id,
                    data={"status": "failed", "error": state_from_graph.error or {"code": "UNKNOWN", "message": "Workflow failed"}},
                )
                await workflow_persistence_manager.update_workflow_status(
                    db, workflow_id, "failed",
                    error=state_from_graph.error or {"code": "UNKNOWN", "message": "Workflow failed"},
                )
                return

            yield PMAgentEvent(
                type="pm_agent_completed", workflowId=workflow_id,
                data={
                    "status": "completed",
                    "finalResponse": state_from_graph.final_response,
                    "completedPhases": state_from_graph.completed_phases,
                    "totalPhases": state_from_graph.total_phases,
                },
            )
            await self._persist(db, state_from_graph)

        except Exception as exc:
            logger.exception("PM Agent execution failed workflow_id=%s", workflow_id)
            yield PMAgentEvent(
                type="pm_agent_failed", workflowId=workflow_id,
                data={"status": "failed", "error": {"code": "EXECUTION_ERROR", "message": str(exc)}},
            )
            await workflow_persistence_manager.update_workflow_status(
                db, workflow_id, "failed",
                error={"code": "EXECUTION_ERROR", "message": str(exc)},
            )

    async def get_status(self, db: AsyncSession, workflow_id: str, user_id: int) -> Optional[PMAgentStatusResponse]:
        workflow = await workflow_persistence_manager.get_workflow(db, workflow_id)
        if not workflow or str(workflow.user_id) != str(user_id):
            return None
        return PMAgentStatusResponse(
            workflowId=workflow.id, status=workflow.status or "unknown",
            finalResponse=workflow.final_response, error=workflow.error_snapshot,
            createdAt=workflow.created_at.isoformat() if workflow.created_at else None,
            updatedAt=workflow.updated_at.isoformat() if workflow.updated_at else None,
        )

    async def _persist(self, db: AsyncSession, state: PMAgentState) -> None:
        try:
            await workflow_persistence_manager.update_workflow_status(
                db, state.workflow_id, state.status,
                final_response=state.final_response, error=state.error,
            )
        except Exception as exc:
            logger.exception("Failed to persist PM agent result: %s", exc)


pm_agent_service = PMAgentService()
