from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic_ai.models.openai import OpenAIChatModel

from app.agent_workflow.state import (
    WorkflowAgentState,
    ToolCallRecord,
    WorkflowStepStatus,
)
from app.skills.contracts import (
    SkillCallRequest,
    SkillContext,
    SkillErrorCode,
    SkillSideEffect,
)
from app.skills.runtime import SkillExecutionContext, SkillRuntime
from app.tool_adapter import (
    ToolAdapterRegistry,
    ToolExecutionRequest,
    ToolExecutor,
    ToolRetryConfig,
    build_tool_adapter_context,
)
from app.tool_adapter.contracts import ToolAdapterContext
from app.services.skill_registry import skill_registry_service

logger = logging.getLogger(__name__)


class ToolExecutionNode:
    def __init__(self) -> None:
        self._executor_cache: dict[str, ToolExecutor] = {}

    async def _build_executor(
        self,
        runtime: SkillRuntime,
    ) -> ToolExecutor:
        cache_key = id(runtime)
        if cache_key not in self._executor_cache:
            from app.tool_adapter import (
                InMemoryToolMetricsSink,
                LoggingLifecycleHook,
                LoggingToolMiddleware,
                MetricsLifecycleHook,
                MetricsToolMiddleware,
            )
            metrics_sink = InMemoryToolMetricsSink()
            self._executor_cache[cache_key] = ToolExecutor(
                runtime,
                middlewares=[
                    LoggingToolMiddleware(logger),
                    MetricsToolMiddleware(metrics_sink),
                ],
                hooks=[
                    LoggingLifecycleHook(logger),
                    MetricsLifecycleHook(metrics_sink),
                ],
            )
        return self._executor_cache[cache_key]

    async def execute_step(
        self,
        state: WorkflowAgentState,
        step_index: int,
        workspace_id: Optional[str],
    ) -> tuple[ToolCallRecord, WorkflowAgentState]:
        step = state.plan.steps[step_index]
        step.status = WorkflowStepStatus.IN_PROGRESS
        step.started_at = datetime.now(timezone.utc).isoformat()
        state.current_step_index = step_index
        state.status = "executing"

        tool_call_id = str(uuid.uuid4())

        try:
            db = state.metadata.get("_db")
            user_id = state.user_id
            if db is None or user_id is None:
                raise RuntimeError("Database session or user_id not available in workflow state")

            registry = await skill_registry_service.build_runtime_registry(
                db,
                workspace_id=workspace_id,
            )
            runtime = SkillRuntime(registry)

            adapter_registry = ToolAdapterRegistry.from_runtime_registry(
                registry,
                skill_names=[step.skill_name] if step.skill_name else None,
            )

            spec = adapter_registry.get_by_skill_name(step.skill_name)
            if spec is None:
                raise RuntimeError(f"Skill not found: {step.skill_name}")

            execution_context = SkillExecutionContext(
                context=SkillContext(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    session_id=state.session_id,
                    conversation_id=state.conversation_id,
                    project_id=state.project_id,
                    table_ids=state.table_ids,
                    view_id=state.view_id,
                    task_id=state.task_id,
                    team_id=state.team_id,
                    organization_id=state.organization_id,
                    workflow_id=state.workflow_id,
                    agent_id=state.agent_id,
                    trace_id=state.workflow_id,
                    origin="workflow",
                    locale=state.locale,
                    timezone=state.timezone,
                    dry_run=False,
                    confirmed=False,
                ),
                db=db,
            )

            executor = await self._build_executor(runtime)

            adapter_context = build_tool_adapter_context(
                execution_context=execution_context,
                skill_name=step.skill_name,
                function_name=spec.function_name,
                runtime_context={
                    "traceId": state.workflow_id,
                    "runtimeMode": "workflow",
                    "workspaceId": workspace_id,
                    "tableIds": state.table_ids,
                    "workflow": {"id": state.workflow_id},
                },
                request_context={
                    "arguments": step.arguments,
                    "confirmed": False,
                },
                state={
                    "metadata": spec.manifest.metadata.model_dump(mode="json"),
                    "schemas": spec.schema_bundle.model_dump(mode="json", by_alias=True),
                },
                agent_context=state.agent_context,
            )

            outcome = await executor.execute(
                request=ToolExecutionRequest(
                    toolCallId=tool_call_id,
                    skillName=step.skill_name,
                    functionName=spec.function_name,
                    arguments=step.arguments,
                    confirmed=False,
                    dry_run=False,
                    traceId=state.workflow_id,
                ),
                execution_context=execution_context,
                adapter_context=adapter_context,
                retry_config=ToolRetryConfig(
                    maxAttempts=step.max_retries,
                    backoffMs=step.retry_delay_ms,
                ),
            )

            tool_response = outcome.response

            record = ToolCallRecord(
                callId=tool_call_id,
                skillName=step.skill_name,
                functionName=spec.function_name,
                arguments=step.arguments,
                state=tool_response.state.value,
                output=tool_response.output,
                error=tool_response.error.model_dump(mode="json") if tool_response.error else None,
                attemptCount=outcome.attempt_count,
                durationMs=round(outcome.duration_ms, 3),
                requiresConfirmation=tool_response.state.value == "requires_confirmation",
                metadata={
                    **tool_response.metadata,
                    "stepIndex": step_index,
                },
            )

            if tool_response.state.value == "completed":
                step.status = WorkflowStepStatus.COMPLETED
                step.result = tool_response.output
            elif tool_response.state.value == "requires_confirmation":
                step.status = WorkflowStepStatus.REQUIRES_APPROVAL
                state.status = "awaiting_approval"
            elif tool_response.state.value == "failed":
                step.status = WorkflowStepStatus.FAILED
                step.error = record.error
                step.retry_count += 1

            step.completed_at = datetime.now(timezone.utc).isoformat()
            state.tool_calls.append(record)

            logger.info(
                "ToolExecutionNode executed step=%d skill=%s state=%s workflow_id=%s",
                step_index,
                step.skill_name,
                record.state,
                state.workflow_id,
            )

            return record, state

        except Exception as exc:
            logger.exception(
                "ToolExecutionNode failed step=%d skill=%s workflow_id=%s",
                step_index,
                step.skill_name,
                state.workflow_id,
            )
            step.status = WorkflowStepStatus.FAILED
            step.error = {"code": "EXECUTION_ERROR", "message": str(exc)}
            step.completed_at = datetime.now(timezone.utc).isoformat()

            record = ToolCallRecord(
                callId=tool_call_id,
                skillName=step.skill_name or "",
                functionName=step.skill_name or "",
                arguments=step.arguments,
                state="failed",
                error={"code": "EXECUTION_ERROR", "message": str(exc)},
                attemptCount=1,
            )
            state.tool_calls.append(record)
            return record, state


tool_execution_node = ToolExecutionNode()
