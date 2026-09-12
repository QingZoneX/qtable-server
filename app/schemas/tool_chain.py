from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ToolChainRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=2, ge=1, le=8, alias="maxAttempts")
    backoff_ms: int = Field(default=500, ge=0, le=10_000, alias="backoffMs")


class ToolChainRollbackPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: Literal["best_effort", "strict"] = "best_effort"


class ToolChainStepRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["none", "delete_created_records", "skill"] = "none"
    skill_name: Optional[str] = Field(default=None, alias="skillName")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolChainStepDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(..., min_length=1, alias="stepId")
    skill_name: str = Field(..., min_length=1, alias="skillName")
    description: str = ""
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    arguments: dict[str, Any] = Field(default_factory=dict)
    save_result_as: Optional[str] = Field(default=None, alias="saveResultAs")
    retry: ToolChainRetryPolicy = Field(default_factory=ToolChainRetryPolicy)
    rollback: ToolChainStepRollback = Field(default_factory=ToolChainStepRollback)
    timeout_seconds: int = Field(default=180, ge=5, le=7200, alias="timeoutSeconds")


class ToolChainPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    summary: str
    reasoning: list[str] = Field(default_factory=list)
    steps: list[ToolChainStepDefinition] = Field(default_factory=list)
    final_output_path: Optional[str] = Field(default=None, alias="finalOutputPath")
    planner_metadata: dict[str, Any] = Field(default_factory=dict, alias="plannerMetadata")
    langgraph_spec: dict[str, Any] = Field(default_factory=dict, alias="langGraphSpec")


class ToolChainContextState(BaseModel):
    model_config = ConfigDict(extra="allow")

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
    variables: dict[str, Any] = Field(default_factory=dict)
    memory: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[dict[str, Any]] = Field(default_factory=list, alias="toolResults")
    notes: list[str] = Field(default_factory=list)
    context_summary: dict[str, Any] = Field(default_factory=dict, alias="contextSummary")
    context_snapshot: dict[str, Any] = Field(default_factory=dict, alias="contextSnapshot")


class ToolChainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1)
    model: Optional[str] = None
    plan: Optional[ToolChainPlan] = None
    auto_plan: bool = Field(default=True, alias="autoPlan")
    dry_run: bool = Field(default=False, alias="dryRun")
    confirmed: bool = False
    stream: bool = False
    persist_run: bool = Field(default=True, alias="persistRun")
    enable_observation: bool = Field(default=True, alias="enableObservation")
    retry: ToolChainRetryPolicy = Field(default_factory=ToolChainRetryPolicy)
    rollback: ToolChainRollbackPolicy = Field(default_factory=ToolChainRollbackPolicy)
    context: ToolChainContextState = Field(default_factory=ToolChainContextState)
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    conversation_id: Optional[str] = Field(default=None, alias="conversationId")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    view_id: Optional[str] = Field(default=None, alias="viewId")
    task_id: Optional[str] = Field(default=None, alias="taskId")
    team_id: Optional[str] = Field(default=None, alias="teamId")
    organization_id: Optional[str] = Field(default=None, alias="organizationId")
    workflow_id: Optional[str] = Field(default=None, alias="workflowId")
    agent_id: Optional[str] = Field(default=None, alias="agentId")
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"


class ToolChainStepState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(alias="stepId")
    skill_name: str = Field(alias="skillName")
    description: str = ""
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    state: Literal[
        "pending",
        "ready",
        "running",
        "retrying",
        "waiting_confirmation",
        "completed",
        "failed",
        "rolled_back",
        "skipped",
    ]
    attempt_count: int = Field(default=0, alias="attemptCount")
    input_payload: dict[str, Any] = Field(default_factory=dict, alias="inputPayload")
    output_payload: Optional[dict[str, Any]] = Field(default=None, alias="outputPayload")
    error: Optional[dict[str, Any]] = None
    rollback: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = Field(default=None, alias="startedAt")
    finished_at: Optional[datetime] = Field(default=None, alias="finishedAt")


class ToolChainEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "run_started",
        "planning_started",
        "planning_completed",
        "step_started",
        "step_completed",
        "step_retrying",
        "step_waiting_confirmation",
        "step_failed",
        "rollback_started",
        "rollback_completed",
        "run_completed",
        "run_failed",
    ]
    run_id: str = Field(alias="runId")
    trace_id: str = Field(alias="traceId")
    step_id: Optional[str] = Field(default=None, alias="stepId")
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )


class ToolChainRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(alias="runId")
    trace_id: str = Field(alias="traceId")
    status: Literal[
        "queued",
        "planning",
        "running",
        "waiting_confirmation",
        "rolling_back",
        "completed",
        "failed",
        "rolled_back",
    ]
    provider: Optional[str] = None
    model: Optional[str] = None
    message: str
    summary: str = ""
    plan: Optional[ToolChainPlan] = None
    context: ToolChainContextState
    steps: list[ToolChainStepState] = Field(default_factory=list)
    memory: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    pending_confirmation: Optional[dict[str, Any]] = Field(default=None, alias="pendingConfirmation")
    error: Optional[dict[str, Any]] = None
    started_at: Optional[datetime] = Field(default=None, alias="startedAt")
    finished_at: Optional[datetime] = Field(default=None, alias="finishedAt")
