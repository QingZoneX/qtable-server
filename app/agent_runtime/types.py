from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentIntent(str, Enum):
    CHAT = "chat"
    ANALYZE = "analyze"
    ACTION = "action"
    MULTI_STEP = "multi_step"


class AgentState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    PREVIEW = "preview"
    AWAITING_CONFIRM = "awaiting_confirm"
    EXECUTING = "executing"
    OBSERVING = "observing"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    intent: AgentIntent = AgentIntent.CHAT
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str = ""
    requires_confirmation: bool = Field(default=False, alias="requiresConfirmation")
    suggested_skills: list[str] = Field(default_factory=list, alias="suggestedSkills")


class ToolPlanStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REQUIRES_APPROVAL = "requires_approval"


class ToolPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    index: int = 0
    description: str = ""
    skill_name: Optional[str] = Field(default=None, alias="skillName")
    function_name: Optional[str] = Field(default=None, alias="functionName")
    arguments: dict[str, Any] = Field(default_factory=dict)

    status: ToolPlanStepStatus = ToolPlanStepStatus.PENDING
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    parallel_group: Optional[int] = Field(default=None, alias="parallelGroup")

    require_approval: bool = Field(default=False, alias="requireApproval")
    max_retries: int = Field(default=3, alias="maxRetries")
    retry_count: int = Field(default=0, alias="retryCount")
    timeout_seconds: int = Field(default=60, alias="timeoutSeconds")

    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    started_at: Optional[str] = Field(default=None, alias="startedAt")
    completed_at: Optional[str] = Field(default=None, alias="completedAt")

    langgraph_node: Optional[str] = Field(default=None, alias="langgraphNode")


class ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = ""
    strategy: str = ""
    reasoning: str = ""
    intent: str = "chat"
    intent_confidence: float = Field(default=0.0, alias="intentConfidence")

    steps: list[ToolPlanStep] = Field(default_factory=list)

    estimated_steps: int = Field(default=0, alias="estimatedSteps")
    requires_confirmation: bool = Field(default=False, alias="requiresConfirmation")
    planner_metadata: dict[str, Any] = Field(default_factory=dict, alias="plannerMetadata")
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    langgraph_spec: Optional[dict[str, Any]] = Field(default=None, alias="langgraphSpec")


class ToolCallState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REQUIRES_CONFIRMATION = "requires_confirmation"


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tool_call_id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="toolCallId")
    step_id: str = Field(alias="stepId")
    skill_name: str = Field(alias="skillName")
    function_name: str = Field(alias="functionName")
    arguments: dict[str, Any] = Field(default_factory=dict)
    state: ToolCallState = ToolCallState.PENDING
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    attempt_count: int = Field(default=0, alias="attemptCount")
    duration_ms: float = Field(default=0.0, alias="durationMs")
    progress: Optional[float] = None
    progress_message: Optional[str] = Field(default=None, alias="progressMessage")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class RecordChange(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    record_id: Optional[str] = Field(default=None, alias="recordId")
    record_title: Optional[str] = Field(default=None, alias="recordTitle")
    changes: dict[str, Any] = Field(default_factory=dict)
    original_values: dict[str, Any] = Field(default_factory=dict, alias="originalValues")


class ActionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    preview_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    step_id: str = Field(alias="stepId")
    skill_name: str = Field(alias="skillName")

    action: Literal[
        "create_record",
        "update_record",
        "delete_record",
        "batch_create",
        "batch_update",
        "batch_delete",
        "custom",
    ]

    summary: str = ""
    affected_count: int = Field(default=0, alias="affectedCount")
    record_changes: list[RecordChange] = Field(default_factory=list, alias="recordChanges")

    table_id: str = Field(alias="tableId")
    table_name: Optional[str] = Field(default=None, alias="tableName")

    is_dangerous: bool = Field(default=False, alias="isDangerous")
    danger_reason: Optional[str] = Field(default=None, alias="dangerReason")

    raw_arguments: dict[str, Any] = Field(default_factory=dict, alias="rawArguments")

    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ConfirmStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    confirm_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    step_id: str = Field(alias="stepId")
    preview: Optional[ActionPreview] = None
    status: ConfirmStatus = ConfirmStatus.PENDING
    timeout_ms: int = Field(default=60000, alias="timeoutMs")
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    resolved_at: Optional[str] = Field(default=None, alias="resolvedAt")


class ObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="observationId")
    source_tool_call_id: str = Field(alias="sourceToolCallId")
    summary: str = ""
    insights: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_steps: int = Field(default=8, ge=1, le=20, alias="maxSteps")
    tool_limit: int = Field(default=16, ge=1, le=32, alias="toolLimit")
    require_confirmation_for_write: bool = Field(
        default=True, alias="requireConfirmationForWrite"
    )
    confirm_timeout_ms: int = Field(default=60000, alias="confirmTimeoutMs")
    max_retries_per_step: int = Field(default=3, alias="maxRetriesPerStep")
    timeout_seconds: int = Field(default=300, alias="timeoutSeconds")
    enable_streaming: bool = Field(default=True, alias="enableStreaming")
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"


class RuntimeContext(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    session_id: Optional[str] = Field(default=None, alias="sessionId")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    conversation_id: Optional[str] = Field(default=None, alias="conversationId")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    view_id: Optional[str] = Field(default=None, alias="viewId")
    task_id: Optional[str] = Field(default=None, alias="taskId")
    team_id: Optional[str] = Field(default=None, alias="teamId")
    organization_id: Optional[str] = Field(default=None, alias="organizationId")
    workflow_id: Optional[str] = Field(default=None, alias="workflowId")
    agent_id: Optional[str] = Field(default=None, alias="agentId")
    user_id: Optional[int] = Field(default=None, alias="userId")
    user_message: str = Field(default="", alias="userMessage")
    messages: list[dict[str, Any]] = Field(default_factory=list)
    config: RuntimeConfig = Field(default_factory=RuntimeConfig)
    agent_context: Optional[dict[str, Any]] = Field(default=None, alias="agentContext")
    context_snapshot: dict[str, Any] = Field(default_factory=dict, alias="contextSnapshot")
    allowed_skills: list[str] = Field(default_factory=list, alias="allowedSkills")
    denied_skills: list[str] = Field(default_factory=list, alias="deniedSkills")
    tool_results: list[dict[str, Any]] = Field(default_factory=list, alias="toolResults")
    variables: dict[str, Any] = Field(default_factory=dict)


class SSERuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal[
        "state",
        "intent",
        "plan",
        "preview",
        "confirm_required",
        "tool_start",
        "tool_progress",
        "tool_end",
        "observation",
        "text",
        "error",
        "done",
    ]
    state: Optional[str] = None
    agent_state: Optional[str] = Field(default=None, alias="agentState")
    intent: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    plan: Optional[ToolPlan] = None
    preview: Optional[ActionPreview] = None
    confirm_id: Optional[str] = Field(default=None, alias="confirmId")
    timeout: Optional[int] = None
    tool_call_id: Optional[str] = Field(default=None, alias="toolCallId")
    skill_name: Optional[str] = Field(default=None, alias="skillName")
    step_id: Optional[str] = Field(default=None, alias="stepId")
    progress: Optional[float] = None
    message: Optional[str] = None
    duration_ms: Optional[float] = Field(default=None, alias="durationMs")
    observation: Optional[ObservationRecord] = None
    content: Optional[str] = None
    done: Optional[bool] = None
    error: Optional[bool] = None
    code: Optional[str] = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ToolPlanEngine:
    @staticmethod
    def get_execution_order(plan: ToolPlan) -> list[list[ToolPlanStep]]:
        step_map = {s.step_id: s for s in plan.steps}
        in_degree: dict[str, int] = {s.step_id: len(s.depends_on) for s in plan.steps}
        batches: list[list[ToolPlanStep]] = []

        remaining = list(plan.steps)
        while remaining:
            batch = [
                s for s in remaining
                if all(
                    d not in step_map or step_map[d].status == ToolPlanStepStatus.COMPLETED
                    for d in s.depends_on
                )
            ]
            if not batch:
                for s in remaining:
                    s.status = ToolPlanStepStatus.FAILED
                    s.error = {"code": "CIRCULAR_DEPENDENCY", "message": "Circular or unresolvable dependency"}
                break
            batches.append(batch)
            for s in batch:
                remaining.remove(s)
        return batches

    @staticmethod
    def validate_plan(plan: ToolPlan) -> list[str]:
        issues: list[str] = []
        step_ids = {s.step_id for s in plan.steps}

        if not plan.goal:
            issues.append("Plan has no goal")
        if not plan.steps:
            issues.append("Plan has no steps")

        for step in plan.steps:
            for dep_id in step.depends_on:
                if dep_id not in step_ids:
                    issues.append(f"Step {step.step_id} depends on unknown step {dep_id}")
            if step.step_id in step.depends_on:
                issues.append(f"Step {step.step_id} depends on itself")
            if not step.skill_name:
                issues.append(f"Step {step.step_id} has no skill_name")

        return issues
