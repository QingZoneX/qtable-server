from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.estimate_workload import TeamCapabilityProfile


class WorkloadPlanningPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(alias="workspaceId")
    table_id: str = Field(alias="tableId")
    record_ids: list[str] = Field(default_factory=list, alias="recordIds")
    deadline: Optional[str] = None
    team_size: int = Field(default=3, ge=1, le=50, alias="teamSize")
    parallel_streams: Optional[int] = Field(default=None, ge=1, le=20, alias="parallelStreams")
    business_domain: Optional[str] = Field(default=None, alias="businessDomain")
    quality_bar: Literal["prototype", "production", "enterprise"] = Field(
        default="production",
        alias="qualityBar",
    )
    source_reference: Optional[dict[str, Any]] = Field(default=None, alias="sourceReference")

    @model_validator(mode="after")
    def normalize_selection(self) -> "WorkloadPlanningPreviewRequest":
        self.record_ids = list(dict.fromkeys(self.record_ids))[:50]
        if self.parallel_streams is None:
            self.parallel_streams = self.team_size
        self.parallel_streams = min(self.parallel_streams, self.team_size)
        return self


class WorkloadPlanningApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(alias="batchId")
    workspace_id: str = Field(alias="workspaceId")
    table_id: str = Field(alias="tableId")
    record_ids: list[str] = Field(default_factory=list, alias="recordIds")


class WorkloadPlanningWhatIfRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(alias="batchId")
    team_size: Optional[int] = Field(default=None, ge=1, le=50, alias="teamSize")
    parallel_streams: Optional[int] = Field(default=None, ge=1, le=20, alias="parallelStreams")
    deadline: Optional[str] = None


class WorkloadPlanningFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(alias="batchId")
    record_id: str = Field(alias="recordId")
    actual_story_points: Optional[float] = Field(default=None, ge=0.0, alias="actualStoryPoints")
    actual_hours: Optional[float] = Field(default=None, ge=0.0, alias="actualHours")
    outcome_status: Literal["better_than_expected", "on_track", "worse_than_expected"] = Field(
        default="on_track",
        alias="outcomeStatus",
    )
    accuracy_rating: Optional[int] = Field(default=None, ge=1, le=5, alias="accuracyRating")
    notes: str = ""
