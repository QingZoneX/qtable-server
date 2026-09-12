from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


VisualTargetType = Literal["auto", "view", "dashboard"]
ViewType = Literal["grid", "board", "gantt", "calendar", "gallery"]
WidgetType = Literal["bar", "line", "pie", "horizontalBar", "table", "metric", "progress"]
Aggregation = Literal["count", "sum", "avg", "max", "min"]


class ViewFilterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(alias="fieldId")
    operator: str
    value: Any = None
    logic: Literal["and", "or", "where"] = "and"


class ViewSortSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(alias="fieldId")
    order: Literal["asc", "desc"] = "asc"


class ViewGroupSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: Optional[str] = Field(default=None, alias="fieldId")
    order: Literal["asc", "desc"] = "asc"


class ViewDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(alias="tableId")
    name: str
    type: ViewType = "grid"
    filters: list[ViewFilterSpec] = Field(default_factory=list, max_length=50)
    sorts: list[ViewSortSpec] = Field(default_factory=list, max_length=20)
    group_config: ViewGroupSpec = Field(
        default_factory=ViewGroupSpec,
        alias="groupConfig",
    )
    visible_field_ids: list[str] = Field(
        default_factory=list,
        alias="visibleFieldIds",
    )
    gantt_config: Optional[dict[str, Any]] = Field(default=None, alias="ganttConfig")
    calendar_config: Optional[dict[str, Any]] = Field(default=None, alias="calendarConfig")
    gallery_config: Optional[dict[str, Any]] = Field(default=None, alias="galleryConfig")


class DashboardMetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aggregation: Aggregation = "count"
    field_id: Optional[str] = Field(default=None, alias="fieldId")


class DashboardWidgetDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    type: WidgetType
    title: str
    table_id: str = Field(alias="tableId")
    dimension_field_id: Optional[str] = Field(default=None, alias="dimensionFieldId")
    metric: DashboardMetricSpec = Field(default_factory=DashboardMetricSpec)
    filters: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    sort: dict[str, Any] = Field(default_factory=lambda: {"by": "value", "order": "desc"})
    limit: int = Field(default=50, ge=1, le=1000)
    layout: dict[str, Any] = Field(default_factory=dict)
    target_value: Optional[float] = Field(default=None, alias="targetValue")
    purpose: str = ""


class DashboardDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    widgets: list[DashboardWidgetDesign] = Field(default_factory=list, min_length=3, max_length=12)


class AiVisualDesignProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["view", "dashboard"]
    rationale: str = ""
    view: Optional[ViewDesign] = None
    dashboard: Optional[DashboardDesign] = None

    @model_validator(mode="after")
    def validate_payload(self) -> "AiVisualDesignProposal":
        if self.kind == "view" and self.view is None:
            raise ValueError("view proposal is required")
        if self.kind == "dashboard" and self.dashboard is None:
            raise ValueError("dashboard proposal is required")
        if self.kind == "view":
            self.dashboard = None
        else:
            self.view = None
        return self


class AiVisualDesignPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(alias="workspaceId")
    prompt: str
    target_type: VisualTargetType = Field(default="auto", alias="targetType")
    table_id: Optional[str] = Field(default=None, alias="tableId")
    parent_id: Optional[str] = Field(default=None, alias="parentId")
    dashboard_id: Optional[str] = Field(default=None, alias="dashboardId")
    current_proposal: Optional[AiVisualDesignProposal] = Field(
        default=None,
        alias="currentProposal",
    )
    instruction: Optional[str] = None
    model: Optional[str] = None

    @model_validator(mode="after")
    def normalize(self) -> "AiVisualDesignPreviewRequest":
        self.workspace_id = self.workspace_id.strip()
        self.prompt = self.prompt.strip()
        self.table_id = self.table_id.strip() if isinstance(self.table_id, str) and self.table_id.strip() else None
        self.parent_id = self.parent_id.strip() if isinstance(self.parent_id, str) and self.parent_id.strip() else None
        self.dashboard_id = self.dashboard_id.strip() if isinstance(self.dashboard_id, str) and self.dashboard_id.strip() else None
        self.instruction = self.instruction.strip() if isinstance(self.instruction, str) and self.instruction.strip() else None
        if not self.workspace_id:
            raise ValueError("workspaceId is required")
        if not self.prompt:
            raise ValueError("prompt is required")
        if len(self.prompt) > 3000:
            raise ValueError("prompt is too long")
        if self.instruction and len(self.instruction) > 2000:
            raise ValueError("instruction is too long")
        if self.dashboard_id and self.target_type == "view":
            raise ValueError("dashboardId cannot be used for a view proposal")
        return self


class AiVisualDesignApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(alias="planId")
    proposal: Optional[AiVisualDesignProposal] = None

    @model_validator(mode="after")
    def normalize(self) -> "AiVisualDesignApplyRequest":
        self.plan_id = self.plan_id.strip()
        if not self.plan_id:
            raise ValueError("planId is required")
        return self
