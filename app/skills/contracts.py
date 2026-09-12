from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SkillSideEffect(str, Enum):
    NONE = "none"
    WRITE = "write"
    EXTERNAL = "external"


class SkillLifecycleState(str, Enum):
    COMPLETED = "completed"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    FAILED = "failed"


class SkillErrorCode(str, Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class SkillPermissionRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: Literal["table", "workspace", "skill", "marketplace"]
    action: Literal["read", "write", "execute", "manage"]
    target_param: Optional[str] = None
    optional: bool = False


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "1.0.0"
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    side_effect: SkillSideEffect = SkillSideEffect.NONE
    confirmation_required: bool = False
    idempotent: bool = True
    supports_dry_run: bool = True
    visibility: Literal["internal", "workspace", "marketplace"] = "internal"
    permissions: list[SkillPermissionRequirement] = Field(default_factory=list)


class SkillContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: Optional[int] = None
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    project_id: Optional[str] = None
    table_ids: list[str] = Field(default_factory=list)
    view_id: Optional[str] = None
    task_id: Optional[str] = None
    team_id: Optional[str] = None
    organization_id: Optional[str] = None
    workflow_id: Optional[str] = None
    agent_id: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    origin: Literal["assistant", "agent", "workflow", "graphql", "manual"] = "assistant"
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    dry_run: bool = False
    confirmed: bool = False


class SkillManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: SkillMetadata
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class SkillCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: Optional[str] = None
    skill_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    confirmed: bool = False
    origin: Literal["assistant", "agent", "workflow", "graphql", "manual"] = "assistant"
    trace_id: Optional[str] = None


class SkillError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: SkillErrorCode
    message: str
    retryable: bool = False
    details: Optional[dict[str, Any]] = None


class SkillCallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    skill_name: str
    state: SkillLifecycleState
    output: Optional[dict[str, Any]] = None
    error: Optional[SkillError] = None
    requires_confirmation: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
