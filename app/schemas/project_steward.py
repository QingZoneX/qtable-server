from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProjectStewardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(alias="workspaceId")
    table_ids: list[str] = Field(alias="tableIds", min_length=1, max_length=8)
    question: str = Field(min_length=1, max_length=2000)
    project_id: Optional[str] = Field(default=None, alias="projectId")
    timezone: str = "Asia/Shanghai"
    due_soon_days: int = Field(default=3, ge=1, le=30, alias="dueSoonDays")
    stale_task_days: int = Field(default=7, ge=1, le=90, alias="staleTaskDays")
    overload_hours: float = Field(default=40.0, gt=0.0, le=500.0, alias="overloadHours")
    overload_window_days: int = Field(default=7, ge=1, le=30, alias="overloadWindowDays")
    max_records: int = Field(default=500, ge=10, le=2000, alias="maxRecords")
    model: Optional[str] = None

    @model_validator(mode="after")
    def normalize_scope(self) -> "ProjectStewardRequest":
        self.table_ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in self.table_ids
                if str(item).strip()
            )
        )[:8]
        if not self.table_ids:
            raise ValueError("At least one task table is required")
        self.question = self.question.strip()
        return self


class ProjectStewardRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordered_conclusion_ids: list[str] = Field(
        default_factory=list,
        alias="orderedConclusionIds",
    )
