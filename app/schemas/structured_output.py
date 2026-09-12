from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StructuredOutputRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=2, ge=1, le=5, alias="maxAttempts")
    max_output_retries: int = Field(default=2, ge=0, le=5, alias="maxOutputRetries")
    max_tool_retries: int = Field(default=1, ge=0, le=3, alias="maxToolRetries")
    backoff_ms: int = Field(default=300, ge=0, le=5000, alias="backoffMs")
    enable_repair: bool = Field(default=True, alias="enableRepair")


class StructuredConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1)
    name: Optional[str] = None


class StructuredEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["tool", "table", "conversation", "model"] = Field(alias="sourceType")
    source_id: str = Field(alias="sourceId")
    quote: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StructuredMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str
    trend: Optional[Literal["up", "down", "flat", "unknown"]] = "unknown"


class StructuredSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    summary: str
    bullets: list[str] = Field(default_factory=list)
    metrics: list[StructuredMetric] = Field(default_factory=list)
    evidence: list[StructuredEvidence] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class StructuredActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str
    priority: Literal["high", "medium", "low"] = "medium"
    owner: Optional[str] = None
    due_at: Optional[str] = Field(default=None, alias="dueAt")


class StructuredRecordField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(alias="fieldId")
    field_name: Optional[str] = Field(default=None, alias="fieldName")
    field_type: Optional[str] = Field(default=None, alias="fieldType")
    value: Any = None
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    rationale: Optional[str] = None


class StructuredRecordDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: Optional[str] = Field(default=None, alias="tableId")
    operation: Literal["create", "update", "upsert"] = "create"
    title: Optional[str] = None
    fields: list[StructuredRecordField] = Field(default_factory=list)
    raw_values: dict[str, Any] = Field(default_factory=dict, alias="rawValues")
    requires_confirmation: bool = Field(default=False, alias="requiresConfirmation")

    @model_validator(mode="after")
    def validate_payload(self) -> "StructuredRecordDraft":
        if self.requires_confirmation and not (self.fields or self.raw_values):
            raise ValueError("recordDraft requires field values when confirmation is needed")
        return self


class StructuredOutputResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "partial", "failed", "requires_confirmation"]
    intent: Literal["analysis", "record_draft", "workflow", "mixed"] = "analysis"
    summary: str
    answer: str
    sections: list[StructuredSection] = Field(default_factory=list)
    actions: list[StructuredActionItem] = Field(default_factory=list)
    record_draft: Optional[StructuredRecordDraft] = Field(default=None, alias="recordDraft")
    warnings: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list, alias="nextSteps")
    evidence: list[StructuredEvidence] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_result(self) -> "StructuredOutputResult":
        if not self.summary.strip():
            raise ValueError("summary must not be empty")
        if not self.answer.strip():
            raise ValueError("answer must not be empty")
        if self.status == "requires_confirmation" and self.record_draft is None:
            raise ValueError("recordDraft is required when status is requires_confirmation")
        return self


class ToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(alias="toolName")
    state: Literal["completed", "failed"]
    attempt: int = 1
    latency_ms: float = Field(alias="latencyMs")
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Optional[dict[str, Any]] = None
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="inputSchema")
    result_schema: dict[str, Any] = Field(default_factory=dict, alias="resultSchema")
    error: Optional[dict[str, Any]] = None


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "error"
    field: Optional[str] = None


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    repaired: bool = False
    recovery_stage: Literal["native", "repaired_json", "failed"] = Field(alias="recoveryStage")
    issues: list[ValidationIssue] = Field(default_factory=list)


class StructuredOutputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    mode: Literal["analysis", "record_draft", "workflow"] = "analysis"
    model: Optional[str] = None
    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    conversation: list[StructuredConversationMessage] = Field(default_factory=list)
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    conversation_id: Optional[str] = Field(default=None, alias="conversationId")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    view_id: Optional[str] = Field(default=None, alias="viewId")
    task_id: Optional[str] = Field(default=None, alias="taskId")
    team_id: Optional[str] = Field(default=None, alias="teamId")
    organization_id: Optional[str] = Field(default=None, alias="organizationId")
    workflow_id: Optional[str] = Field(default=None, alias="workflowId")
    agent_id: Optional[str] = Field(default=None, alias="agentId")
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    use_tools: bool = Field(default=True, alias="useTools")
    stream: bool = False
    retry: StructuredOutputRetryPolicy = Field(default_factory=StructuredOutputRetryPolicy)


class StructuredOutputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(alias="traceId")
    provider: str
    model: str
    attempts: int
    result: StructuredOutputResult
    validation: ValidationReport
    tool_observations: list[ToolObservation] = Field(default_factory=list, alias="toolObservations")
    raw_response_text: Optional[str] = Field(default=None, alias="rawResponseText")
    error: Optional[dict[str, Any]] = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )


class StructuredOutputStreamEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["status", "partial", "tool", "final", "error"]
    trace_id: str = Field(alias="traceId")
    data: dict[str, Any] = Field(default_factory=dict)
