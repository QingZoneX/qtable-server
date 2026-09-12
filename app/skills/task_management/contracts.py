"""
Task Management Agent Tool System - Contracts & Metadata

定义五层工具架构的类型系统、工具元数据、JSON Schema 规范。
每个工具同时输出 OpenAI、MCP、PydanticAI 三套 Schema。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============ Tool Layer Architecture ============

class TaskManagementToolLayer(str, Enum):
    """五层工具架构"""
    ENVIRONMENT = "environment"          # Layer 1: 环境感知
    SCHEMA = "schema"                     # Layer 2: Schema 感知
    DOMAIN_INTELLIGENCE = "domain_intelligence"  # Layer 3: 领域智能
    WORKFLOW = "workflow"                 # Layer 4: 工作流
    AI_RUNTIME = "ai_runtime"            # Layer 5: AI Runtime


class ToolDomain(str, Enum):
    """工具所属领域"""
    ENVIRONMENT = "environment"
    SCHEMA = "schema"
    TASK = "task"
    PROJECT = "project"
    MEMBER = "member"
    WORKLOAD = "workload"
    DEPENDENCY = "dependency"
    RISK = "risk"
    PLAN = "plan"
    EXECUTION = "execution"


class ToolIntelligenceLevel(str, Enum):
    """工具智能等级"""
    PASSIVE = "passive"       # 被动查询，直接返回数据
    ANALYTICAL = "analytical"  # 分析级别，包含推理
    PREDICTIVE = "predictive"  # 预测级别，包含模型预测
    PRESCRIPTIVE = "prescriptive"  # 建议级别，包含执行建议


class TaskManagementToolOperateMode(str, Enum):
    """工具操作模式"""
    SYNC = "sync"           # 同步执行
    ASYNC_STREAM = "async_stream"  # 异步流式
    BATCH = "batch"         # 批处理
    CACHED = "cached"       # 缓存优先


# ============ Enhanced Tool Metadata ============

class MCPCompatibilityInfo(BaseModel):
    """MCP 协议兼容信息"""
    model_config = ConfigDict(extra="forbid")

    mcp_version: str = Field(default="1.0", alias="mcpVersion")
    transport: Literal["stdio", "sse", "streamable_http"] = "sse"
    capabilities: list[str] = Field(default_factory=list)
    annotations: dict[str, Any] = Field(default_factory=dict)


class LangGraphCompatibilityInfo(BaseModel):
    """LangGraph 兼容信息"""
    model_config = ConfigDict(extra="forbid")

    can_be_node: bool = Field(default=True, alias="canBeNode")
    node_type: Literal["tool", "conditional_edge", "subgraph"] = "tool"
    input_key: str = "input"
    output_key: str = "output"
    supports_human_in_the_loop: bool = Field(default=False, alias="supportsHumanInTheLoop")


class MultiAgentInfo(BaseModel):
    """Multi-Agent 支持信息"""
    model_config = ConfigDict(extra="forbid")

    agent_scoped: bool = Field(default=True, alias="agentScoped")
    sharing_mode: Literal["isolated", "shared_read", "shared_read_write"] = "isolated"
    inter_agent_communication: bool = Field(default=False, alias="interAgentCommunication")
    required_roles: list[str] = Field(default_factory=list, alias="requiredRoles")


class ToolObservability(BaseModel):
    """工具可观测性配置"""
    model_config = ConfigDict(extra="forbid")

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    metrics_enabled: bool = Field(default=True, alias="metricsEnabled")
    tracing_enabled: bool = Field(default=True, alias="tracingEnabled")
    sensitive_fields: list[str] = Field(default_factory=list, alias="sensitiveFields")
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0, alias="sampleRate")


class TaskManagementToolMetadata(BaseModel):
    """
    增强版工具元数据，扩展自 SkillMetadata。

    在 SkillMetadata 基础上增加:
    - 分层信息 (layer)
    - 领域信息 (domain)
    - 智能等级 (intelligence_level)
    - 操作模式 (operate_mode)
    - MCP 兼容性
    - LangGraph 兼容性
    - Multi-Agent 支持
    - 可观测性配置
    - 缓存策略
    - 分页支持
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # --- 继承 SkillMetadata ---
    name: str
    version: str = "1.0.0"
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    side_effect: Literal["none", "write", "external"] = "none"
    confirmation_required: bool = Field(default=False, alias="confirmationRequired")
    idempotent: bool = True
    supports_dry_run: bool = Field(default=True, alias="supportsDryRun")
    visibility: Literal["internal", "workspace", "marketplace"] = "internal"

    # --- 分层架构 ---
    layer: TaskManagementToolLayer
    domain: ToolDomain
    intelligence_level: ToolIntelligenceLevel = Field(
        default=ToolIntelligenceLevel.PASSIVE,
        alias="intelligenceLevel",
    )

    # --- 操作模式 ---
    operate_mode: TaskManagementToolOperateMode = Field(
        default=TaskManagementToolOperateMode.SYNC,
        alias="operateMode",
    )
    supports_streaming: bool = Field(default=False, alias="supportsStreaming")
    supports_batch: bool = Field(default=False, alias="supportsBatch")
    supports_pagination: bool = Field(default=False, alias="supportsPagination")

    # --- 企业级兼容性 ---
    mcp: MCPCompatibilityInfo = Field(default_factory=MCPCompatibilityInfo)
    langgraph: LangGraphCompatibilityInfo = Field(default_factory=LangGraphCompatibilityInfo)
    multi_agent: MultiAgentInfo = Field(default_factory=MultiAgentInfo)

    # --- 可观测性 ---
    observability: ToolObservability = Field(default_factory=ToolObservability)

    # --- 缓存策略 ---
    cache_ttl_seconds: int = Field(default=60, ge=0, le=3600, alias="cacheTtlSeconds")
    cache_strategy: Literal["none", "simple", "stale_while_revalidate"] = Field(
        default="simple",
        alias="cacheStrategy",
    )
    cache_key_template: Optional[str] = Field(default=None, alias="cacheKeyTemplate")

    # --- 超时与重试 ---
    timeout_seconds: int = Field(default=30, ge=5, le=300, alias="timeoutSeconds")
    max_retries: int = Field(default=2, ge=0, le=5, alias="maxRetries")
    retry_backoff_ms: int = Field(default=350, ge=0, le=5000, alias="retryBackoffMs")

    # --- 依赖声明 ---
    depends_on_tools: list[str] = Field(default_factory=list, alias="dependsOnTools")
    conflicts_with_tools: list[str] = Field(default_factory=list, alias="conflictsWithTools")

    # --- 分页配置 ---
    default_page_size: int = Field(default=50, ge=10, le=500, alias="defaultPageSize")
    max_page_size: int = Field(default=200, ge=50, le=1000, alias="maxPageSize")


# ============ Common Input/Output Models ============

class PaginationParams(BaseModel):
    """分页参数"""
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200, alias="pageSize")


class PaginationInfo(BaseModel):
    """分页信息"""
    model_config = ConfigDict(extra="forbid")

    page: int
    page_size: int = Field(alias="pageSize")
    total_count: int = Field(alias="totalCount")
    total_pages: int = Field(alias="totalPages")
    has_more: bool = Field(alias="hasMore")


class DateRange(BaseModel):
    """日期范围"""
    model_config = ConfigDict(extra="forbid")

    start_date: Optional[str] = Field(default=None, alias="startDate")
    end_date: Optional[str] = Field(default=None, alias="endDate")


class SortConfig(BaseModel):
    """排序配置"""
    model_config = ConfigDict(extra="forbid")

    field: str
    direction: Literal["asc", "desc"] = "desc"


class FilterCondition(BaseModel):
    """过滤条件"""
    model_config = ConfigDict(extra="forbid")

    field: str
    operator: Literal["eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "is_null", "is_not_null"]
    value: Any = None


# ============ JSON Schema Generation ============

class ToolSchemaOutput(BaseModel):
    """统一的工具 Schema 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    function_name: str = Field(alias="functionName")
    description: str
    json_schema: dict[str, Any] = Field(alias="jsonSchema")
    openai_tool_schema: dict[str, Any] = Field(alias="openaiToolSchema")
    mcp_tool_schema: dict[str, Any] = Field(alias="mcpToolSchema")
    pydantic_ai_schema: dict[str, Any] = Field(alias="pydanticAiSchema")
    langgraph_node_spec: dict[str, Any] = Field(alias="langgraphNodeSpec")
    metadata: TaskManagementToolMetadata


# ============ Tool Execution Context ============

class TaskManagementToolContext(BaseModel):
    """任务管理工具执行上下文"""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    user_id: Optional[int] = Field(default=None, alias="userId")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    table_id: Optional[str] = Field(default=None, alias="tableId")
    task_id: Optional[str] = Field(default=None, alias="taskId")
    team_id: Optional[str] = Field(default=None, alias="teamId")
    organization_id: Optional[str] = Field(default=None, alias="organizationId")
    agent_id: Optional[str] = Field(default=None, alias="agentId")
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    conversation_id: Optional[str] = Field(default=None, alias="conversationId")
    trace_id: Optional[str] = Field(default=None, alias="traceId")
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    dry_run: bool = False
    confirmed: bool = False

    # 工具链上下文
    tool_chain_depth: int = Field(default=0, alias="toolChainDepth")
    previous_results: list[dict[str, Any]] = Field(default_factory=list, alias="previousResults")
    variables: dict[str, Any] = Field(default_factory=dict)


# ============ Tool Execution Result ============

class ToolExecutionMetrics(BaseModel):
    """工具执行指标"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tool_name: str = Field(alias="toolName")
    layer: TaskManagementToolLayer
    duration_ms: float = Field(alias="durationMs")
    cache_hit: bool = Field(default=False, alias="cacheHit")
    retry_count: int = Field(default=0, alias="retryCount")
    db_query_count: int = Field(default=0, alias="dbQueryCount")
    db_query_duration_ms: float = Field(default=0.0, alias="dbQueryDurationMs")
    records_processed: int = Field(default=0, alias="recordsProcessed")
    token_estimate: Optional[int] = Field(default=None, alias="tokenEstimate")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        alias="timestamp",
    )


class ToolResult(BaseModel):
    """统一的工具执行结果"""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    success: bool
    tool_name: str = Field(alias="toolName")
    layer: TaskManagementToolLayer
    data: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    pagination: Optional[PaginationInfo] = None
    metrics: ToolExecutionMetrics
    cached: bool = False
    from_fallback: bool = Field(default=False, alias="fromFallback")
    observations: list[str] = Field(default_factory=list)
