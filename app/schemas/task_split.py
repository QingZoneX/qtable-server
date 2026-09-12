from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskSplitRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=2, ge=1, le=5, alias="maxAttempts")
    max_output_retries: int = Field(default=2, ge=0, le=5, alias="maxOutputRetries")
    max_tool_retries: int = Field(default=1, ge=0, le=3, alias="maxToolRetries")
    backoff_ms: int = Field(default=300, ge=0, le=5000, alias="backoffMs")


class TaskSplitEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    optimistic_hours: float = Field(default=0.0, ge=0.0, alias="optimisticHours")
    likely_hours: float = Field(default=0.0, ge=0.0, alias="likelyHours")
    pessimistic_hours: float = Field(default=0.0, ge=0.0, alias="pessimisticHours")
    buffered_hours: float = Field(default=0.0, ge=0.0, alias="bufferedHours")


class TaskSplitRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    level: Literal["low", "medium", "high", "critical"] = "medium"
    impact: str
    mitigation: str


class TaskSplitDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predecessor_key: str = Field(alias="predecessorKey")
    successor_key: str = Field(alias="successorKey")
    dependency_type: Literal["blocks", "relates_to", "parallelizable"] = Field(
        default="blocks",
        alias="dependencyType",
    )
    rationale: str = ""


class TaskSplitNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = ""
    title: str
    description: str = ""
    objective: str = ""
    depth: int = 0
    estimate: TaskSplitEstimate = Field(default_factory=TaskSplitEstimate)
    risks: list[TaskSplitRisk] = Field(default_factory=list)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    milestone: str = ""
    suggested_role: str = Field(default="", alias="suggestedRole")
    source_reference: Optional[dict[str, Any]] = Field(default=None, alias="sourceReference")
    deliverables: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list, alias="acceptanceCriteria")
    required_skills: list[str] = Field(default_factory=list, alias="requiredSkills")
    tags: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    children: list["TaskSplitNode"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_node(self) -> "TaskSplitNode":
        if not self.title.strip():
            raise ValueError("node title must not be empty")
        return self


class TaskSplitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    summary: str
    assumptions: list[str] = Field(default_factory=list)
    risks: list[TaskSplitRisk] = Field(default_factory=list)
    root: TaskSplitNode
    dependencies: list[TaskSplitDependency] = Field(default_factory=list)
    mermaid: str = ""
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan(self) -> "TaskSplitPlan":
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if not self.summary.strip():
            raise ValueError("summary must not be empty")
        return self


class TaskSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    model: Optional[str] = None
    auto_create_records: bool = Field(default=True, alias="autoCreateRecords")
    dry_run: bool = Field(default=False, alias="dryRun")
    max_depth: int = Field(default=3, ge=1, le=6, alias="maxDepth")
    max_children_per_node: int = Field(default=8, ge=1, le=12, alias="maxChildrenPerNode")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    conversation_id: Optional[str] = Field(default=None, alias="conversationId")
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
    retry: TaskSplitRetryPolicy = Field(default_factory=TaskSplitRetryPolicy)


class TaskSplitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(alias="traceId")
    provider: str
    model: str
    attempts: int
    persisted: bool
    plan_id: Optional[str] = Field(default=None, alias="planId")
    result: TaskSplitPlan
    error: Optional[dict] = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )


TaskSplitNode.model_rebuild()
