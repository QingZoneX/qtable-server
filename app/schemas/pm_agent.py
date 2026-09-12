from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.agent_workflow.pm_agent.state import (
    RequirementItem, ModuleItem, TaskTreeNode, WorkloadEstimateItem,
    MemberAssignment, MilestoneItem, GanttTask, RiskItem,
    WorkflowDesign, ProjectReport, PMPhaseResult,
)


class PMAgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1)
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
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    streaming_enabled: bool = Field(default=True, alias="streamingEnabled")


class PMAgentPhaseInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str
    status: str
    started_at: Optional[str] = Field(default=None, alias="startedAt")
    completed_at: Optional[str] = Field(default=None, alias="completedAt")
    data: Optional[dict] = None
    error: Optional[dict] = None


class PMAgentStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(alias="workflowId")
    status: str
    pm_phase: Optional[str] = Field(default=None, alias="pmPhase")
    phase_sequence: list[str] = Field(default_factory=list, alias="phaseSequence")
    completed_phases: int = Field(default=0, alias="completedPhases")
    total_phases: int = Field(default=0, alias="totalPhases")
    phase_results: list[PMAgentPhaseInfo] = Field(default_factory=list, alias="phaseResults")
    requirements: list[dict] = Field(default_factory=list)
    modules: list[dict] = Field(default_factory=list)
    task_tree: list[dict] = Field(default_factory=list, alias="taskTree")
    workload_estimates: list[dict] = Field(default_factory=list, alias="workloadEstimates")
    member_assignments: list[dict] = Field(default_factory=list, alias="memberAssignments")
    milestones: list[dict] = Field(default_factory=list)
    gantt_tasks: list[dict] = Field(default_factory=list, alias="ganttTasks")
    risks: list[dict] = Field(default_factory=list)
    project_report: Optional[dict] = Field(default=None, alias="projectReport")
    final_response: Optional[str] = Field(default=None, alias="finalResponse")
    error: Optional[dict] = None
    created_at: Optional[str] = Field(default=None, alias="createdAt")
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")


class PMAgentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflows: list[PMAgentStatusResponse]
    total: int
    offset: int = 0
    limit: int = 20
