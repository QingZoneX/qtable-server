from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ContextBaseModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class EntityContext(ContextBaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TableContext(EntityContext):
    field_count: int = Field(default=0, alias="fieldCount")
    record_count: int = Field(default=0, alias="recordCount")
    sample_fields: list[dict[str, Any]] = Field(default_factory=list, alias="sampleFields")
    sample_views: list[dict[str, Any]] = Field(default_factory=list, alias="sampleViews")


class ViewContext(EntityContext):
    table_id: Optional[str] = Field(default=None, alias="tableId")
    type: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)


class ConversationTurn(ContextBaseModel):
    role: str
    content: str
    created_at: Optional[str] = Field(default=None, alias="createdAt")
    table_ids: list[str] = Field(default_factory=list, alias="tableIds")


class ConversationCompression(ContextBaseModel):
    total_messages: int = Field(default=0, alias="totalMessages")
    kept_messages: int = Field(default=0, alias="keptMessages")
    compressed_messages: int = Field(default=0, alias="compressedMessages")
    original_chars: int = Field(default=0, alias="originalChars")
    compressed_chars: int = Field(default=0, alias="compressedChars")
    strategy: str = "tail-window+heuristic-summary"


class ConversationContext(EntityContext):
    title: Optional[str] = None
    recent_turns: list[ConversationTurn] = Field(default_factory=list, alias="recentTurns")
    summary: str = ""
    message_count: int = Field(default=0, alias="messageCount")
    compression: ConversationCompression = Field(default_factory=ConversationCompression)


class SessionContext(EntityContext):
    session_key: Optional[str] = Field(default=None, alias="sessionKey")
    cache_hit: bool = Field(default=False, alias="cacheHit")
    summary: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    expires_at: Optional[str] = Field(default=None, alias="expiresAt")


class WorkflowContext(EntityContext):
    current_step: Optional[str] = Field(default=None, alias="currentStep")
    status: Optional[str] = None


class AgentNodeContext(EntityContext):
    kind: str = "router"
    capabilities: list[str] = Field(default_factory=list)


class ContextWindow(ContextBaseModel):
    target_chars: int = Field(default=0, alias="targetChars")
    used_chars: int = Field(default=0, alias="usedChars")
    remaining_chars: int = Field(default=0, alias="remainingChars")
    cache_key: str = Field(default="", alias="cacheKey")


class ContextSummary(ContextBaseModel):
    text: str
    highlights: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class AgentContext(ContextBaseModel):
    scope_key: str = Field(alias="scopeKey")
    user: EntityContext = Field(default_factory=EntityContext)
    project: EntityContext = Field(default_factory=EntityContext)
    workspace: EntityContext = Field(default_factory=EntityContext)
    table: TableContext = Field(default_factory=TableContext)
    task: EntityContext = Field(default_factory=EntityContext)
    team: EntityContext = Field(default_factory=EntityContext)
    organization: EntityContext = Field(default_factory=EntityContext)
    view: ViewContext = Field(default_factory=ViewContext)
    session: SessionContext = Field(default_factory=SessionContext)
    conversation: ConversationContext = Field(default_factory=ConversationContext)
    workflow: WorkflowContext = Field(default_factory=WorkflowContext)
    agents: list[AgentNodeContext] = Field(default_factory=list)
    summary: ContextSummary
    window: ContextWindow
    generated_at: datetime = Field(alias="generatedAt")


class ContextBuildInput(ContextBaseModel):
    source: str = "api"
    user_id: Optional[int] = Field(default=None, alias="userId")
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    conversation_id: Optional[str] = Field(default=None, alias="conversationId")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    view_id: Optional[str] = Field(default=None, alias="viewId")
    task_id: Optional[str] = Field(default=None, alias="taskId")
    team_id: Optional[str] = Field(default=None, alias="teamId")
    organization_id: Optional[str] = Field(default=None, alias="organizationId")
    workflow_id: Optional[str] = Field(default=None, alias="workflowId")
    agent_id: Optional[str] = Field(default=None, alias="agentId")
    agent_stack: list[AgentNodeContext] = Field(default_factory=list, alias="agentStack")
    message: Optional[str] = None
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    metadata: dict[str, Any] = Field(default_factory=dict)
