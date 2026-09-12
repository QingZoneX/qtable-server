from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.task_split import TaskSplitPlan


class TaskPlanningPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    workspace_id: str = Field(alias="workspaceId")
    target_table_id: str = Field(alias="targetTableId")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    parent_task_id: Optional[str] = Field(default=None, alias="parentTaskId")
    selected_record_ids: list[str] = Field(default_factory=list, alias="selectedRecordIds")
    source_reference: Optional[dict[str, Any]] = Field(default=None, alias="sourceReference")
    deadline: Optional[str] = None
    team_size: Optional[int] = Field(default=None, ge=1, le=200, alias="teamSize")
    granularity: Literal["coarse", "balanced", "fine"] = "balanced"
    max_depth: int = Field(default=3, ge=2, le=4, alias="maxDepth")
    max_children_per_node: int = Field(default=8, ge=2, le=10, alias="maxChildrenPerNode")
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"

    @model_validator(mode="after")
    def normalize_limits(self) -> "TaskPlanningPreviewRequest":
        if self.granularity == "coarse":
            self.max_depth = min(self.max_depth, 2)
            self.max_children_per_node = min(self.max_children_per_node, 6)
        elif self.granularity == "fine":
            self.max_depth = max(self.max_depth, 4)
        self.selected_record_ids = list(dict.fromkeys(self.selected_record_ids))[:50]
        return self


class TaskPlanningDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_key: str = Field(alias="nodeKey")
    action: Literal["create", "reuse", "merge", "skip"] = "create"
    record_id: Optional[str] = Field(default=None, alias="recordId")

    @model_validator(mode="after")
    def validate_record_action(self) -> "TaskPlanningDecision":
        if self.action in {"reuse", "merge"} and not self.record_id:
            raise ValueError("recordId is required for reuse/merge")
        return self


class TaskPlanningApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(alias="planId")
    workspace_id: str = Field(alias="workspaceId")
    target_table_id: str = Field(alias="targetTableId")
    plan: TaskSplitPlan
    decisions: list[TaskPlanningDecision] = Field(default_factory=list)
    allow_repeat: bool = Field(default=False, alias="allowRepeat")

    @model_validator(mode="after")
    def unique_decisions(self) -> "TaskPlanningApplyRequest":
        seen: set[str] = set()
        for decision in self.decisions:
            if decision.node_key in seen:
                raise ValueError(f"duplicate decision for node {decision.node_key}")
            seen.add(decision.node_key)
        return self
