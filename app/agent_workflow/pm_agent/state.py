from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.agent_workflow.state import (
    WorkflowAgentState,
    WorkflowConfig,
    WorkflowStep,
    HumanApprovalRequest,
    ObservationRecord,
    ToolCallRecord,
)


class PMPhase(str, Enum):
    REQUIREMENT_ANALYSIS = "requirement_analysis"
    MODULE_SPLIT = "module_split"
    TASK_TREE = "task_tree"
    WORKLOAD_ESTIMATE = "workload_estimate"
    MEMBER_ASSIGNMENT = "member_assignment"
    MILESTONE = "milestone"
    GANTT = "gantt"
    RISK_ANALYSIS = "risk_analysis"
    WORKFLOW_DESIGN = "workflow_design"
    REPORT = "report"


class PMPhaseStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REQUIRES_APPROVAL = "requires_approval"


class RequirementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str = ""
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    category: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list, alias="acceptanceCriteria")
    dependencies: list[str] = Field(default_factory=list)


class ModuleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str = ""
    parent_module_id: Optional[str] = Field(default=None, alias="parentModuleId")
    order: int = 0
    responsibilities: list[str] = Field(default_factory=list)
    sub_modules: list[str] = Field(default_factory=list, alias="subModules")


class TaskTreeNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str = ""
    module_id: Optional[str] = Field(default=None, alias="moduleId")
    parent_id: Optional[str] = Field(default=None, alias="parentId")
    depth: int = 0
    estimate_hours: float = Field(default=0.0, ge=0.0, alias="estimateHours")
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    status: str = "backlog"
    assignee_id: Optional[int] = Field(default=None, alias="assigneeId")
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    children: list[TaskTreeNode] = Field(default_factory=list)


class WorkloadEstimateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_name: str = Field(alias="moduleName")
    story_points: float = Field(default=0.0, ge=0.0, alias="storyPoints")
    p50_hours: float = Field(default=0.0, ge=0.0, alias="p50Hours")
    p90_hours: float = Field(default=0.0, ge=0.0, alias="p90Hours")
    risk_coefficient: float = Field(default=1.0, ge=0.5, le=3.0, alias="riskCoefficient")
    assigned_members: int = Field(default=1, alias="assignedMembers")


class MemberAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(alias="userId")
    name: str = ""
    role: str = ""
    module_ids: list[str] = Field(default_factory=list, alias="moduleIds")
    task_ids: list[str] = Field(default_factory=list, alias="taskIds")
    total_hours: float = Field(default=0.0, alias="totalHours")
    utilization: float = Field(default=0.0, ge=0.0, le=1.0)


class MilestoneItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str = ""
    due_date: Optional[str] = Field(default=None, alias="dueDate")
    module_ids: list[str] = Field(default_factory=list, alias="moduleIds")
    deliverables: list[str] = Field(default_factory=list)
    status: str = "planned"


class GanttTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    start_date: Optional[str] = Field(default=None, alias="startDate")
    end_date: Optional[str] = Field(default=None, alias="endDate")
    duration_days: int = Field(default=0, alias="durationDays")
    progress: float = Field(default=0.0, ge=0.0, le=100.0)
    dependencies: list[str] = Field(default_factory=list)
    assignee: Optional[str] = None
    milestone_id: Optional[str] = Field(default=None, alias="milestoneId")
    color: str = "#4A90D9"


class RiskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str = ""
    category: str = ""
    level: Literal["low", "medium", "high", "critical"] = "medium"
    probability: float = Field(default=0.0, ge=0.0, le=1.0)
    impact: float = Field(default=0.0, ge=0.0, le=1.0)
    mitigation: str = ""
    contingency: str = ""
    owner: Optional[str] = None
    status: str = "open"


class WorkflowDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stages: list[dict[str, Any]] = Field(default_factory=list)
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    roles: list[dict[str, Any]] = Field(default_factory=list)
    automation_rules: list[dict[str, Any]] = Field(default_factory=list, alias="automationRules")


class ProjectReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executive_summary: str = Field(alias="executiveSummary")
    scope: str = ""
    modules: list[dict[str, Any]] = Field(default_factory=list)
    timeline: str = ""
    total_estimated_hours: float = Field(default=0.0, alias="totalEstimatedHours")
    team_size: int = Field(default=0, alias="teamSize")
    risks_summary: list[str] = Field(default_factory=list, alias="risksSummary")
    milestones: list[dict[str, Any]] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list, alias="nextSteps")
    chart_urls: dict[str, str] = Field(default_factory=dict, alias="chartUrls")


class PMPhaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: PMPhase
    status: PMPhaseStatus
    started_at: Optional[str] = Field(default=None, alias="startedAt")
    completed_at: Optional[str] = Field(default=None, alias="completedAt")
    error: Optional[dict[str, Any]] = None
    data: Any = None
    token_usage: dict[str, int] = Field(default_factory=dict, alias="tokenUsage")


class PMAgentMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(alias="sessionId")
    short_term: list[dict[str, Any]] = Field(default_factory=list, alias="shortTerm")
    long_term: list[dict[str, Any]] = Field(default_factory=list, alias="longTerm")
    embeddings: Optional[list[float]] = None
    context_summary: str = Field(default="", alias="contextSummary")
    key_decisions: list[dict[str, Any]] = Field(default_factory=list, alias="keyDecisions")
    user_preferences: dict[str, Any] = Field(default_factory=dict, alias="userPreferences")


class PMAgentState(WorkflowAgentState):
    model_config = ConfigDict(extra="allow")

    pm_phase: Optional[PMPhase] = Field(default=None, alias="pmPhase")
    phase_sequence: list[PMPhase] = Field(default_factory=list, alias="phaseSequence")
    phase_results: list[PMPhaseResult] = Field(default_factory=list, alias="phaseResults")
    current_phase_index: int = Field(default=-1, alias="currentPhaseIndex")

    requirements: list[RequirementItem] = Field(default_factory=list)
    modules: list[ModuleItem] = Field(default_factory=list)
    task_tree: list[TaskTreeNode] = Field(default_factory=list)
    workload_estimates: list[WorkloadEstimateItem] = Field(default_factory=list, alias="workloadEstimates")
    member_assignments: list[MemberAssignment] = Field(default_factory=list, alias="memberAssignments")
    milestones: list[MilestoneItem] = Field(default_factory=list)
    gantt_tasks: list[GanttTask] = Field(default_factory=list, alias="ganttTasks")
    risks: list[RiskItem] = Field(default_factory=list)
    workflow_design: Optional[WorkflowDesign] = Field(default=None, alias="workflowDesign")
    project_report: Optional[ProjectReport] = Field(default=None, alias="projectReport")

    agent_memory: PMAgentMemory = Field(default_factory=lambda: PMAgentMemory(sessionId=""), alias="agentMemory")

    total_phases: int = Field(default=10, alias="totalPhases")
    completed_phases: int = Field(default=0, alias="completedPhases")

    streaming_enabled: bool = Field(default=True, alias="streamingEnabled")
    auto_approve_readonly: bool = Field(default=True, alias="autoApproveReadonly")
