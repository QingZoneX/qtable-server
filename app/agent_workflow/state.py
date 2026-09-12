from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field


class WorkflowStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REQUIRES_APPROVAL = "requires_approval"


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    index: int = 0
    description: str = ""
    skill_name: Optional[str] = Field(default=None, alias="skillName")
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: WorkflowStepStatus = WorkflowStepStatus.PENDING
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    max_retries: int = Field(default=3, alias="maxRetries")
    retry_count: int = Field(default=0, alias="retryCount")
    retry_delay_ms: int = Field(default=1000, alias="retryDelayMs")
    require_approval: bool = Field(default=False, alias="requireApproval")
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    started_at: Optional[str] = Field(default=None, alias="startedAt")
    completed_at: Optional[str] = Field(default=None, alias="completedAt")


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = ""
    strategy: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    reasoning: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="callId")
    skill_name: str = Field(alias="skillName")
    function_name: str = Field(alias="functionName")
    arguments: dict[str, Any] = Field(default_factory=dict)
    state: str = "pending"
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    attempt_count: int = Field(default=0, alias="attemptCount")
    duration_ms: float = Field(default=0.0, alias="durationMs")
    requires_confirmation: bool = Field(default=False, alias="requiresConfirmation")
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), alias="observationId"
    )
    source_call_id: Optional[str] = Field(default=None, alias="sourceCallId")
    summary: str = ""
    insights: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class HumanApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), alias="approvalId"
    )
    call_id: str = Field(alias="callId")
    skill_name: str = Field(alias="skillName")
    function_name: str = Field(alias="functionName")
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    preview: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    resolved_at: Optional[str] = Field(default=None, alias="resolvedAt")


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=10, ge=1, le=50, alias="maxSteps")
    max_retries_per_step: int = Field(default=3, ge=0, le=10, alias="maxRetriesPerStep")
    timeout_seconds: int = Field(default=300, ge=10, le=3600, alias="timeoutSeconds")
    require_approval_for_write: bool = Field(
        default=True, alias="requireApprovalForWrite"
    )
    parallel_tool_execution: bool = Field(
        default=False, alias="parallelToolExecution"
    )
    enable_streaming: bool = Field(default=True, alias="enableStreaming")


class WorkflowAgentState(BaseModel):
    model_config = ConfigDict(extra="allow")

    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()), alias="workflowId")
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    user_id: Optional[int] = Field(default=None, alias="userId")
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

    user_message: str = Field(default="", alias="userMessage")
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"

    messages: list[dict[str, Any]] = Field(default_factory=list)
    plan: Optional[WorkflowPlan] = None
    current_step_index: int = Field(default=-1, alias="currentStepIndex")

    tool_calls: list[ToolCallRecord] = Field(default_factory=list, alias="toolCalls")
    observations: list[ObservationRecord] = Field(default_factory=list)
    pending_approvals: list[HumanApprovalRequest] = Field(
        default_factory=list, alias="pendingApprovals"
    )
    approval_history: list[HumanApprovalRequest] = Field(
        default_factory=list, alias="approvalHistory"
    )

    status: Literal[
        "initializing", "planning", "executing", "observing",
        "awaiting_approval", "retrying", "completed", "failed", "cancelled",
    ] = "initializing"

    final_response: Optional[str] = Field(default=None, alias="finalResponse")
    error: Optional[dict[str, Any]] = None

    config: WorkflowConfig = Field(default_factory=WorkflowConfig)
    context_snapshot: dict[str, Any] = Field(default_factory=dict, alias="contextSnapshot")
    agent_context: Optional[dict[str, Any]] = Field(default=None, alias="agentContext")

    retry_policy: dict[str, Any] = Field(default_factory=dict, alias="retryPolicy")
    allowed_skills: list[str] = Field(default_factory=list, alias="allowedSkills")
    denied_skills: list[str] = Field(default_factory=list, alias="deniedSkills")

    metadata: dict[str, Any] = Field(default_factory=dict)

    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
