from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EstimateWorkloadRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=2, ge=1, le=5, alias="maxAttempts")
    max_output_retries: int = Field(default=2, ge=0, le=5, alias="maxOutputRetries")
    max_tool_retries: int = Field(default=1, ge=0, le=3, alias="maxToolRetries")
    backoff_ms: int = Field(default=400, ge=0, le=5000, alias="backoffMs")


class HistoricalLearningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    limit: int = Field(default=6, ge=1, le=20)
    lookback_days: int = Field(default=180, ge=7, le=1095, alias="lookbackDays")
    require_feedback: bool = Field(default=False, alias="requireFeedback")


class TeamCapabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_size: int = Field(default=4, ge=1, le=50, alias="teamSize")
    avg_seniority: Literal["junior", "mixed", "senior", "staff_plus"] = Field(
        default="mixed",
        alias="avgSeniority",
    )
    domain_familiarity: float = Field(default=0.7, ge=0.0, le=1.0, alias="domainFamiliarity")
    stack_familiarity: float = Field(default=0.7, ge=0.0, le=1.0, alias="stackFamiliarity")
    delivery_maturity: float = Field(default=0.7, ge=0.0, le=1.0, alias="deliveryMaturity")
    parallel_streams: int = Field(default=2, ge=1, le=12, alias="parallelStreams")


class TechStackProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_stack: list[str] = Field(default_factory=list, alias="primaryStack")
    architecture: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    legacy_surface: str = Field(default="medium", alias="legacySurface")


class WorkloadRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    level: Literal["low", "medium", "high", "critical"] = "medium"
    impact: str
    mitigation: str


class WorkloadBreakdownItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    story_points: float = Field(default=0.0, ge=0.0, alias="storyPoints")
    p50_hours: float = Field(default=0.0, ge=0.0, alias="p50Hours")
    p90_hours: float = Field(default=0.0, ge=0.0, alias="p90Hours")
    risk_coefficient: float = Field(default=1.0, ge=0.5, le=3.0, alias="riskCoefficient")
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_breakdown(self) -> "WorkloadBreakdownItem":
        if self.p90_hours < self.p50_hours:
            raise ValueError("p90Hours must be greater than or equal to p50Hours")
        if not self.name.strip():
            raise ValueError("breakdown item name must not be empty")
        return self


class HistoricalEstimateSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimate_id: str = Field(alias="estimateId")
    title: str
    similarity: float = Field(ge=0.0, le=1.0)
    source_type: Literal["feedback", "estimate"] = Field(alias="sourceType")
    estimated_story_points: Optional[float] = Field(default=None, alias="estimatedStoryPoints")
    actual_story_points: Optional[float] = Field(default=None, alias="actualStoryPoints")
    estimated_hours: Optional[float] = Field(default=None, alias="estimatedHours")
    actual_hours: Optional[float] = Field(default=None, alias="actualHours")
    summary: str = ""
    tech_stack: list[str] = Field(default_factory=list, alias="techStack")


class HistoricalLearningSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(default=0, alias="sampleCount")
    feedback_sample_count: int = Field(default=0, alias="feedbackSampleCount")
    average_hours_per_story_point: float = Field(
        default=0.0,
        ge=0.0,
        alias="averageHoursPerStoryPoint",
    )
    average_bias: float = Field(default=1.0, ge=0.0, alias="averageBias")
    notes: list[str] = Field(default_factory=list)
    examples: list[HistoricalEstimateSignal] = Field(default_factory=list)


class EstimateConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(default=0.0, ge=0.0, le=1.0)
    level: Literal["low", "medium", "high"] = "medium"
    rationale: str = ""


class ContextInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = ""
    signals: list[str] = Field(default_factory=list)


class EstimateWorkloadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    scope: str
    base_story_points: float = Field(default=0.0, ge=0.0, alias="baseStoryPoints")
    adjusted_story_points: float = Field(default=0.0, ge=0.0, alias="adjustedStoryPoints")
    p50_hours: float = Field(default=0.0, ge=0.0, alias="p50Hours")
    p90_hours: float = Field(default=0.0, ge=0.0, alias="p90Hours")
    risk_coefficient: float = Field(default=1.0, ge=0.5, le=3.0, alias="riskCoefficient")
    team_capability_factor: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        alias="teamCapabilityFactor",
    )
    tech_stack_factor: float = Field(default=1.0, ge=0.5, le=2.0, alias="techStackFactor")
    historical_adjustment_factor: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        alias="historicalAdjustmentFactor",
    )
    confidence: EstimateConfidence = Field(default_factory=EstimateConfidence)
    context_insight: ContextInsight = Field(default_factory=ContextInsight, alias="contextInsight")
    historical_learning: HistoricalLearningSummary = Field(
        default_factory=HistoricalLearningSummary,
        alias="historicalLearning",
    )
    breakdown: list[WorkloadBreakdownItem] = Field(default_factory=list)
    top_risks: list[WorkloadRisk] = Field(default_factory=list, alias="topRisks")
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    formula: str = ""
    next_actions: list[str] = Field(default_factory=list, alias="nextActions")

    @property
    def story_points(self) -> float:
        return self.adjusted_story_points

    @model_validator(mode="after")
    def validate_result(self) -> "EstimateWorkloadResult":
        if self.p90_hours < self.p50_hours:
            raise ValueError("p90Hours must be greater than or equal to p50Hours")
        if self.adjusted_story_points < self.base_story_points * 0.4:
            raise ValueError("adjustedStoryPoints is unrealistically lower than baseStoryPoints")
        if not self.summary.strip():
            raise ValueError("summary must not be empty")
        if not self.scope.strip():
            raise ValueError("scope must not be empty")
        if not self.breakdown:
            raise ValueError("breakdown must contain at least one item")
        return self


class EstimateWorkloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    model: Optional[str] = None
    persist_result: bool = Field(default=True, alias="persistResult")
    dry_run: bool = Field(default=False, alias="dryRun")
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
    team_profile: TeamCapabilityProfile = Field(default_factory=TeamCapabilityProfile, alias="teamProfile")
    tech_stack: TechStackProfile = Field(default_factory=TechStackProfile, alias="techStack")
    business_domain: Optional[str] = Field(default=None, alias="businessDomain")
    quality_bar: Literal["prototype", "production", "enterprise"] = Field(
        default="production",
        alias="qualityBar",
    )
    non_functional_requirements: list[str] = Field(
        default_factory=list,
        alias="nonFunctionalRequirements",
    )
    reference_urls: list[str] = Field(default_factory=list, alias="referenceUrls")
    tags: list[str] = Field(default_factory=list)
    historical_learning: HistoricalLearningConfig = Field(
        default_factory=HistoricalLearningConfig,
        alias="historicalLearning",
    )
    retry: EstimateWorkloadRetryPolicy = Field(default_factory=EstimateWorkloadRetryPolicy)


class EstimateWorkloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(alias="traceId")
    provider: str
    model: str
    attempts: int
    persisted: bool
    estimate_id: Optional[str] = Field(default=None, alias="estimateId")
    result: EstimateWorkloadResult
    error: Optional[dict] = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )


class EstimateFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_story_points: Optional[float] = Field(default=None, ge=0.0, alias="actualStoryPoints")
    actual_hours: Optional[float] = Field(default=None, ge=0.0, alias="actualHours")
    outcome_status: Literal["better_than_expected", "on_track", "worse_than_expected"] = Field(
        default="on_track",
        alias="outcomeStatus",
    )
    accuracy_rating: Optional[int] = Field(default=None, ge=1, le=5, alias="accuracyRating")
    notes: str = ""
