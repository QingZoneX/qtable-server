from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_workflow.state import (
    WorkflowAgentState,
    WorkflowConfig,
    HumanApprovalRequest,
)
from app.agent_workflow.graph import agent_workflow_graph
from app.agent_workflow.persistence import (
    workflow_persistence_manager,
    redis_session_manager,
)
from app.agent_workflow.nodes.human_approval import human_approval_node
from app.context_engine.models import AgentContext
from app.models.agent_workflow import AgentWorkflow as AgentWorkflowModel

logger = logging.getLogger(__name__)


class WorkflowRunRequest(BaseModel):
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
    allowed_skills: list[str] = Field(default_factory=list, alias="allowedSkills")
    denied_skills: list[str] = Field(default_factory=list, alias="deniedSkills")
    config: Optional[WorkflowConfig] = None
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"


class WorkflowResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(..., alias="workflowId")
    approval_id: str = Field(..., alias="approvalId")
    approved: bool = False


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    workflow_id: str = Field(alias="workflowId")
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class WorkflowStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(alias="workflowId")
    status: str
    plan: Optional[dict[str, Any]] = None
    steps_completed: int = Field(default=0, alias="stepsCompleted")
    steps_total: int = Field(default=0, alias="stepsTotal")
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, alias="toolCalls")
    observations: list[dict[str, Any]] = Field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list, alias="pendingApprovals")
    final_response: Optional[str] = Field(default=None, alias="finalResponse")
    error: Optional[dict[str, Any]] = None
    created_at: Optional[str] = Field(default=None, alias="createdAt")
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")


class AgentWorkflowService:
    async def run_workflow(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: WorkflowRunRequest,
        model,
        agent_context: Optional[AgentContext] = None,
    ) -> AsyncIterator[WorkflowEvent]:
        workflow_id = str(uuid.uuid4())
        config = request.config or WorkflowConfig()

        state = WorkflowAgentState(
            workflowId=workflow_id,
            sessionId=request.session_id,
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
            allowedSkills=request.allowed_skills,
            deniedSkills=request.denied_skills,
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
            logger.warning("Redis connect failed, continuing without Redis: %s", exc)

        try:
            await workflow_persistence_manager.create_workflow(
                db,
                workflow_id=workflow_id,
                user_id=user_id,
                session_id=request.session_id,
                conversation_id=request.conversation_id,
                workspace_id=request.workspace_id,
                table_ids=request.table_ids,
                user_message=request.message,
                config_snapshot=config.model_dump(mode="json", by_alias=True),
                context_snapshot=agent_context.model_dump(mode="json", by_alias=True) if agent_context else {},
                retry_policy={"maxRetriesPerStep": config.max_retries_per_step},
                agent_context=agent_context.model_dump(mode="json", by_alias=True) if agent_context else None,
                project_id=request.project_id,
                view_id=request.view_id,
                task_id=request.task_id,
                team_id=request.team_id,
                organization_id=request.organization_id,
                workflow_def_id=request.workflow_def_id,
                agent_id=request.agent_id,
            )
        except Exception as exc:
            logger.exception("Failed to create workflow record: %s", exc)

        yield WorkflowEvent(
            type="workflow_started",
            workflowId=workflow_id,
            data={"message": request.message, "status": "initializing"},
        )

        compiled = agent_workflow_graph.compiled
        langgraph_config = {
            "configurable": {
                "thread_id": workflow_id,
                "checkpoint_ns": "agent_workflow",
            }
        }

        try:
            final_state = None
            async for event in compiled.astream(
                state.model_dump(mode="json", by_alias=True),
                config=langgraph_config,
                stream_mode="values",
            ):
                if isinstance(event, dict):
                    final_state = event
                    yield WorkflowEvent(
                        type="workflow_step",
                        workflowId=workflow_id,
                        data={
                            "status": event.get("status", "executing"),
                            "currentStepIndex": event.get("currentStepIndex", -1),
                            "toolCalls": len(event.get("toolCalls", [])),
                            "observations": len(event.get("observations", [])),
                        },
                    )
                else:
                    final_state = event

            if final_state:
                state_from_graph = WorkflowAgentState.model_validate(final_state)
            else:
                state_from_graph = state
                state_from_graph.status = "completed"

            if state_from_graph.status == "awaiting_approval":
                yield WorkflowEvent(
                    type="requires_approval",
                    workflowId=workflow_id,
                    data={
                        "status": "awaiting_approval",
                        "pendingApprovals": [
                            a.model_dump(mode="json", by_alias=True)
                            for a in state_from_graph.pending_approvals
                        ],
                    },
                )
                await self._persist_workflow_result(db, state_from_graph)
                return

            if state_from_graph.status == "failed":
                yield WorkflowEvent(
                    type="workflow_failed",
                    workflowId=workflow_id,
                    data={
                        "status": "failed",
                        "error": state_from_graph.error or {"code": "UNKNOWN", "message": "Workflow failed"},
                    },
                )
                await self._persist_workflow_result(db, state_from_graph)
                return

            yield WorkflowEvent(
                type="workflow_completed",
                workflowId=workflow_id,
                data={
                    "status": "completed",
                    "finalResponse": state_from_graph.final_response,
                    "stepsCompleted": sum(
                        1 for s in (state_from_graph.plan.steps if state_from_graph.plan else [])
                        if s.status.value == "completed"
                    ),
                    "stepsTotal": len(state_from_graph.plan.steps) if state_from_graph.plan else 0,
                },
            )

            await self._persist_workflow_result(db, state_from_graph)

        except Exception as exc:
            logger.exception("Workflow execution failed workflow_id=%s", workflow_id)
            yield WorkflowEvent(
                type="workflow_failed",
                workflowId=workflow_id,
                data={"status": "failed", "error": {"code": "EXECUTION_ERROR", "message": str(exc)}},
            )
            await workflow_persistence_manager.update_workflow_status(
                db,
                workflow_id,
                "failed",
                error={"code": "EXECUTION_ERROR", "message": str(exc)},
            )

    async def resume_workflow(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        workflow_id: str,
        approval_id: str,
        approved: bool,
        model,
    ) -> AsyncIterator[WorkflowEvent]:
        workflow_record = await workflow_persistence_manager.get_workflow(db, workflow_id)
        if not workflow_record:
            yield WorkflowEvent(
                type="error",
                workflowId=workflow_id,
                data={"code": "NOT_FOUND", "message": "Workflow not found"},
            )
            return

        if str(workflow_record.user_id) != str(user_id):
            yield WorkflowEvent(
                type="error",
                workflowId=workflow_id,
                data={"code": "FORBIDDEN", "message": "Not authorized to resume this workflow"},
            )
            return

        state = WorkflowAgentState(
            workflowId=workflow_id,
            sessionId=workflow_record.session_id,
            userId=user_id,
            conversationId=workflow_record.conversation_id,
            workspaceId=workflow_record.workspace_id,
            tableIds=json.loads(workflow_record.table_ids) if workflow_record.table_ids else [],
            userMessage=workflow_record.user_message or "",
            status="executing",
            metadata={
                "_db": db,
                "_model": model,
            },
        )

        if workflow_record.plan_snapshot:
            from app.agent_workflow.state import WorkflowPlan
            state.plan = WorkflowPlan.model_validate(workflow_record.plan_snapshot)
        if workflow_record.pending_approvals_snapshot:
            for a in workflow_record.pending_approvals_snapshot:
                state.pending_approvals.append(HumanApprovalRequest.model_validate(a))
        if workflow_record.tool_calls_snapshot:
            from app.agent_workflow.state import ToolCallRecord
            for tc in workflow_record.tool_calls_snapshot:
                state.tool_calls.append(ToolCallRecord.model_validate(tc))
        if workflow_record.observations_snapshot:
            from app.agent_workflow.state import ObservationRecord
            for o in workflow_record.observations_snapshot:
                state.observations.append(ObservationRecord.model_validate(o))

        state = await human_approval_node.process_approval(
            state,
            approval_id,
            approved,
            db,
        )

        await workflow_persistence_manager.update_workflow_status(db, workflow_id, "executing")

        yield WorkflowEvent(
            type="workflow_resumed",
            workflowId=workflow_id,
            data={"status": "executing", "approvalId": approval_id, "approved": approved},
        )

        compiled = agent_workflow_graph.compiled
        langgraph_config = {
            "configurable": {
                "thread_id": workflow_id,
                "checkpoint_ns": "agent_workflow",
            }
        }

        try:
            final_state = None
            async for event in compiled.astream(
                state.model_dump(mode="json", by_alias=True),
                config=langgraph_config,
                stream_mode="values",
            ):
                if isinstance(event, dict):
                    final_state = event
                    yield WorkflowEvent(
                        type="workflow_step",
                        workflowId=workflow_id,
                        data={
                            "status": event.get("status", "executing"),
                            "currentStepIndex": event.get("currentStepIndex", -1),
                        },
                    )
                else:
                    final_state = event

            if final_state:
                state_from_graph = WorkflowAgentState.model_validate(final_state)
            else:
                state_from_graph = state
                state_from_graph.status = "completed"

            if state_from_graph.status == "awaiting_approval":
                yield WorkflowEvent(
                    type="requires_approval",
                    workflowId=workflow_id,
                    data={
                        "status": "awaiting_approval",
                        "pendingApprovals": [
                            a.model_dump(mode="json", by_alias=True)
                            for a in state_from_graph.pending_approvals
                        ],
                    },
                )
                await self._persist_workflow_result(db, state_from_graph)
                return

            yield WorkflowEvent(
                type="workflow_completed",
                workflowId=workflow_id,
                data={
                    "status": "completed",
                    "finalResponse": state_from_graph.final_response,
                },
            )
            await self._persist_workflow_result(db, state_from_graph)

        except Exception as exc:
            logger.exception("Workflow resume failed workflow_id=%s", workflow_id)
            yield WorkflowEvent(
                type="workflow_failed",
                workflowId=workflow_id,
                data={"status": "failed", "error": {"code": "EXECUTION_ERROR", "message": str(exc)}},
            )

    async def get_workflow_status(
        self,
        db: AsyncSession,
        workflow_id: str,
    ) -> Optional[WorkflowStatusResponse]:
        workflow = await workflow_persistence_manager.get_workflow(db, workflow_id)
        if not workflow:
            return None

        plan = None
        if workflow.plan_snapshot:
            plan = workflow.plan_snapshot
            steps_total = len(plan.get("steps", []))
            steps_completed = sum(
                1 for s in plan.get("steps", [])
                if s.get("status") in {"completed", "skipped"}
            )
        else:
            steps_total = 0
            steps_completed = 0

        return WorkflowStatusResponse(
            workflowId=workflow.id,
            status=workflow.status,
            plan=plan,
            stepsCompleted=steps_completed,
            stepsTotal=steps_total,
            toolCalls=workflow.tool_calls_snapshot or [],
            observations=workflow.observations_snapshot or [],
            pendingApprovals=workflow.pending_approvals_snapshot or [],
            finalResponse=workflow.final_response,
            error=workflow.error_snapshot,
            createdAt=workflow.created_at.isoformat() if workflow.created_at else None,
            updatedAt=workflow.updated_at.isoformat() if workflow.updated_at else None,
        )

    async def _persist_workflow_result(
        self,
        db: AsyncSession,
        state: WorkflowAgentState,
    ) -> None:
        try:
            await workflow_persistence_manager.update_workflow_status(
                db,
                state.workflow_id,
                state.status,
                final_response=state.final_response,
                error=state.error,
            )
            await workflow_persistence_manager.save_checkpoint(
                db,
                state.workflow_id,
                {
                    "checkpoint_id": str(uuid.uuid4()),
                    "plan": state.plan.model_dump(mode="json", by_alias=True) if state.plan else None,
                    "tool_calls": [
                        tc.model_dump(mode="json", by_alias=True) for tc in state.tool_calls
                    ],
                    "observations": [
                        o.model_dump(mode="json", by_alias=True) for o in state.observations
                    ],
                    "current_step_index": state.current_step_index,
                    "status": state.status,
                    "pending_approvals": [
                        a.model_dump(mode="json", by_alias=True) for a in state.pending_approvals
                    ],
                    "approval_history": [
                        a.model_dump(mode="json", by_alias=True) for a in state.approval_history
                    ],
                    "final_response": state.final_response,
                },
            )
        except Exception as exc:
            logger.exception("Failed to persist workflow result: %s", exc)


agent_workflow_service = AgentWorkflowService()
