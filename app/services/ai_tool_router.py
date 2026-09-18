from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.models import AgentContext
from app.core.config import settings
from app.models.ai_config import AiConfig
from app.services.deepseek_helper import (
    DEEPSEEK_BASE_URL,
    build_deepseek_model,
    is_deepseek_model,
    is_deepseek_v4_model,
)
from app.services.encryption import decrypt_api_key
from app.services.skill_registry import skill_registry_service
from app.tool_adapter import (
    InMemoryToolMetricsSink,
    LoggingLifecycleHook,
    LoggingToolMiddleware,
    MetricsLifecycleHook,
    MetricsToolMiddleware,
    ToolAdapterRegistry,
    ToolExecutionRequest,
    ToolExecutor,
    ToolRetryConfig,
    build_tool_adapter_context,
)
from app.skills.contracts import (
    SkillContext,
    SkillErrorCode,
    SkillManifestEntry,
    SkillSideEffect,
)
from app.skills.runtime import SkillExecutionContext, SkillRuntime
from app.services.ai_chat_logger import log_chat_conversation

logger = logging.getLogger(__name__)


ROUTER_SYSTEM_PROMPT = """你是 QTable 的 Tool Router / Agent Runtime。

你的职责：
1. 理解用户意图，并在必要时调用合适的 Skill。
2. 优先选择最少但足够的工具链，避免无意义调用。
3. 工具参数必须严格遵循输入 Schema。
4. 工具执行失败时，优先根据错误修正参数，而不是编造结果。
5. 读取类 Skill 可直接调用；写入类 / 外部副作用 Skill 若要求确认，必须停止并返回 requires_confirmation。
6. 可以多轮调用多个 Skill，直到拿到足够结果再回答。
7. 最终回答必须基于真实 Tool 输出，不能伪造记录、统计或执行结果。

**重要的工具调用规则**：
- 在进行任何时间相关分析前（如判断任务延期、计算工期、预测截止日期等），**必须先调用 get_current_datetime 获取当前真实日期**。绝不要假设或编造当前日期。
- 在查询表数据前，如果对表结构（字段名称、类型、选项ID）不确定，**应先调用 describe_table_schema 或 qtable.table.describe 了解表结构**，再填充参数。
- 如果用户的问题是"分析延期情况"或"哪些任务延期了"等，你的调用链应该是：
  1. get_current_datetime（获取当前日期）
  2. describe_table_schema 或 qtable.table.describe（了解表结构，获取延期状态字段信息）
  3. get_overdue_tasks（查询延期任务）
  按顺序依次调用，每一步的结果都是下一步的前提。

**记录创建数据格式规范**（写入数据时必须严格遵守）：
- values 字典的 key 必须是字段的 id（如 f1、f6），从 qtable.table.describe 返回结果中获取
- date 类型字段的值必须是**毫秒时间戳整数**（如 1776873600000），严禁使用 ISO 日期字符串
- member 类型字段的值必须是**数组格式**（如 ["u1"]），严禁使用单字符串
- select 类型字段的值必须是选项的 id 字符串（如 "opt1"），不能使用 label
- 创建单条记录使用 qtable.record.create，批量创建任务使用 qtable.task.records.create

最终输出要求：
- 必须返回结构化结果，包含 `status` 和 `answer`。
- `status` 只能是 `completed`、`requires_confirmation`、`failed`。
- 如果任一工具要求确认，`status` 必须为 `requires_confirmation`。
- 如果没有可用工具或任务无法完成，`status` 应为 `failed` 或明确说明限制。
"""


RECORD_CREATE_SYSTEM_APPEND = """当前任务是"根据用户自然语言生成待确认的新记录草稿"。

**必须严格遵循以下执行流程，不得跳过任何步骤：**

### 第一步：获取目标表的完整 Schema
- **必须先调用 `qtable.table.describe`**，传入正确的 tableId，获取该表中的所有字段信息。
- 仔细阅读返回结果中的 `fields` 列表。每个字段包含：
  - `id`: 字段的唯一标识（如 `fld_xxxxx`）—— **这是你创建记录时 values 字典的 key**
  - `name`: 字段的显示名称（如 "任务名称"、"状态"）—— 用于理解字段含义
  - `type`: 字段类型（text, number, select, multiSelect, date, member, progress, rating, checkbox, url, email, phone, attachment）
  - `options` / `enumValues`: 对于 select/multiSelect 类型，包含可选值列表，每个选项有 `id` 和 `label`
  - `property`: 字段属性（number 的前缀/后缀/precision，rating 的 max）

### 第二步：根据 Schema 和用户需求生成记录数据
- **values 字典的 key 必须使用字段的 `id`（如 `fld_xxxxx`），绝不要使用字段的 `name` 或自己编造字段名（如 title、assignee）。**
- 根据字段类型生成正确格式的值：
  - **select**: 值为选项的 `id` 字符串（如 `"opt1"`），不要用 description 或 label
  - **multiSelect**: 值为选项 `id` 的数组（如 `["opt1", "opt2"]`）
  - **member**: 值为用户 ID 的**数组**（如 `["u1"]`），**必须用数组格式，不能用单字符串**
  - **date**: 值为毫秒时间戳**整数**（如 `1776873600000`），**绝不能使用 ISO 字符串格式**
  - **number**: 值为数字（如 `2000`）
  - **progress**: 值为 0-100 的数字（如 `26.3`）
  - **rating**: 值为整数，不超过 property.max（通常为 5，如 `2`）
  - **text/email/phone/url**: 值为字符串
  - **checkbox**: 值为布尔值

📋 **关键格式示例** - 假设字段 id 为 `f1`(text)、`f2`(select)、`f3`(member)、`f6`(date)：
```json
{
  "values": {
    "f1": "客户数据清洗与整理",
    "f2": "opt1",
    "f3": ["u1"],
    "f6": 1776873600000
  }
}
```

- 从用户请求中提取信息，未提到的字段不传值（留空）。
- 如果用户要求批量创建多条记录（如"创建10个任务"），生成所有记录。
- **使用 `qtable.record.create` 创建记录，不要使用其他工具。**

### 第三步：调用 `qtable.record.create` 生成记录
- 传入 `tableId` 和 `values`（字段 ID 为 key，对应值）
- 每条记录单独调用一次 `qtable.record.create`
- 该工具已设置 `confirmation_required=true`，会在生成草稿后请求确认
- **一旦工具返回 requires_confirmation，立即停止继续调用工具，并输出 `requires_confirmation` 状态**

### 第四步：汇总结果
- answer 中简要说明已生成的记录数量和内容摘要，等待用户确认。
- 如果生成了多条记录，在 answer 中说明"共生成 N 条记录"。
"""


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _tokenize_for_match(value: str) -> set[str]:
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{1,4}", (value or "").lower())
    return {item for item in parts if item}


class ToolRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=2, ge=1, le=5, alias="maxAttempts")
    backoff_ms: int = Field(default=350, ge=0, le=5_000, alias="backoffMs")


class ToolContextState(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: Optional[str] = Field(default=None, alias="sessionId")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    conversation_id: Optional[str] = Field(default=None, alias="conversationId")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    view_id: Optional[str] = Field(default=None, alias="viewId")
    task_id: Optional[str] = Field(default=None, alias="taskId")
    team_id: Optional[str] = Field(default=None, alias="teamId")
    organization_id: Optional[str] = Field(default=None, alias="organizationId")
    variables: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[dict[str, Any]] = Field(default_factory=list, alias="toolResults")
    notes: list[str] = Field(default_factory=list)
    chain_depth: int = Field(default=0, ge=0, alias="chainDepth")
    retry_policy: ToolRetryPolicy = Field(default_factory=ToolRetryPolicy, alias="retryPolicy")
    workflow: dict[str, Any] = Field(default_factory=dict)
    agent_stack: list[dict[str, Any]] = Field(default_factory=list, alias="agentStack")
    context_summary: dict[str, Any] = Field(default_factory=dict, alias="contextSummary")
    context_snapshot: dict[str, Any] = Field(default_factory=dict, alias="contextSnapshot")
    pending_confirmation: Optional[dict[str, Any]] = Field(
        default=None,
        alias="pendingConfirmation",
    )


class ToolRouterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
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
    workflow_id: Optional[str] = Field(default=None, alias="workflowId")
    agent_id: Optional[str] = Field(default=None, alias="agentId")
    conversation: list[dict[str, Any]] = Field(default_factory=list)
    tool_context: ToolContextState = Field(default_factory=ToolContextState, alias="toolContext")
    allowed_skills: list[str] = Field(default_factory=list, alias="allowedSkills")
    denied_skills: list[str] = Field(default_factory=list, alias="deniedSkills")
    max_steps: int = Field(default=6, ge=1, le=12, alias="maxSteps")
    tool_limit: int = Field(default=12, ge=1, le=32, alias="toolLimit")
    confirmed: bool = False
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"


class ToolExecutionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(..., alias="toolCallId")
    skill_name: str = Field(..., alias="skillName")
    function_name: str = Field(..., alias="functionName")
    arguments: dict[str, Any]
    attempt_count: int = Field(default=1, alias="attemptCount")
    state: str
    output: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RouterUsageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: Optional[int] = Field(default=None, alias="promptTokens")
    completion_tokens: Optional[int] = Field(default=None, alias="completionTokens")
    total_tokens: Optional[int] = Field(default=None, alias="totalTokens")


class ToolRouterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "requires_confirmation", "failed"]
    answer: str
    provider: str
    model: str
    trace_id: str = Field(..., alias="traceId")
    steps: int
    tool_calls: list[ToolExecutionTrace] = Field(default_factory=list, alias="toolCalls")
    tool_context: ToolContextState = Field(alias="toolContext")
    usage: Optional[RouterUsageInfo] = None
    pending_confirmation: Optional[dict[str, Any]] = Field(default=None, alias="pendingConfirmation")
    error: Optional[dict[str, Any]] = None


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    base_url: str = Field(alias="baseUrl")


class AgentStructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "requires_confirmation", "failed"]
    answer: str


@dataclass
class AgentRuntimeDeps:
    tool_executor: ToolExecutor
    tool_adapter_registry: ToolAdapterRegistry
    execution_context: SkillExecutionContext
    tool_context: ToolContextState
    tool_runtime_context: dict[str, Any]
    retry_policy: ToolRetryPolicy
    confirmed: bool
    trace_id: str
    agent_context: AgentContext | None = None
    traces: list[ToolExecutionTrace] = field(default_factory=list)


class ToolRouterService:
    # 仅 deepseek-reasoner (R1) 不支持工具调用（reasoner API 完全不支持 function calling）
    # deepseek-v4-* (Pro/Flash) 在非思考模式下支持标准工具调用，
    # 思考模式下由 deepseek_helper 模块在 HTTP Transport 层自动处理 tool_choice 移除
    _TOOL_CALLING_UNSUPPORTED_MODELS = {
        "deepseek-reasoner",
    }

    async def load_user_ai_config(self, db: AsyncSession, user_id: int) -> AiConfig:
        result = await db.execute(select(AiConfig).where(AiConfig.user_id == str(user_id)))
        config = result.scalars().first()
        if not config:
            raise ValueError("AI configuration not found")
        return config

    @staticmethod
    def is_v4_model(model_name: str | None) -> bool:
        """判断是否为 DeepSeek V4 系列模型（需要特殊 tool_choice 处理）。"""
        return is_deepseek_v4_model(model_name)

    def model_supports_tool_calling(self, model_name: str | None) -> bool:
        normalized = (model_name or "").strip().lower()
        if not normalized:
            return True
        if normalized in self._TOOL_CALLING_UNSUPPORTED_MODELS:
            return False
        if normalized.startswith("deepseek-reasoner"):
            return False
        # V4 模型支持工具调用（非思考模式原生支持，思考模式由 transport 层处理）
        return True

    def is_tool_calling_unsupported_error(self, message: str | None) -> bool:
        normalized = (message or "").strip().lower()
        if not normalized:
            return False
        tool_markers = ("tool_choice", "tools", "tool calling", "function calling")
        unsupported_markers = ("does not support", "unsupported", "invalid_request_error")
        # 复合匹配：工具标记 + 不支持标记 同时出现
        if any(marker in normalized for marker in tool_markers) and any(
            marker in normalized for marker in unsupported_markers
        ):
            return True
        # DeepSeek V4 特有的错误码：MODEL_UNSUPPORTED_FOR_AGENT
        if "model_unsupported_for_agent" in normalized:
            return True
        return False

    async def build_provider_model(
        self,
        config: AiConfig,
        *,
        model_override: str | None = None,
    ) -> tuple[OpenAIChatModel, ProviderConfig]:
        provider = (config.provider or "deepseek").strip().lower()
        api_key = decrypt_api_key(config.api_key_encrypted)
        if provider == "openai":
            base_url = (settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            model_name = model_override or config.model or settings.OPENAI_MODEL or "gpt-4o-mini"
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
            model = OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(openai_client=client),
            )
        elif provider in {"deepseek", "deepseek-chat", "deepseek-reasoner"}:
            model_name = model_override or config.model or settings.DEEPSEEK_MODEL or "deepseek-chat"
            base_url = (settings.DEEPSEEK_BASE_URL or DEEPSEEK_BASE_URL).rstrip("/")
            # 使用 build_deepseek_model 以获得 V4 模型的兼容处理
            model = build_deepseek_model(api_key, model_name, base_url)
            provider = "deepseek"
        else:
            base_url = (
                settings.OPENAI_BASE_URL
                or settings.DEEPSEEK_BASE_URL
                or "https://api.openai.com/v1"
            ).rstrip("/")
            model_name = (
                model_override
                or config.model
                or settings.OPENAI_MODEL
                or settings.DEEPSEEK_MODEL
                or "gpt-4o-mini"
            )
            provider = "openai-compatible"
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
            model = OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(openai_client=client),
            )

        return model, ProviderConfig(provider=provider, model=model_name, baseUrl=base_url)

    # 环境感知和 Schema 层的关键工具名称 —— 这些是分析推理的基础，必须始终可用
    _CRITICAL_UTILITY_TOOL_PREFIXES = (
        "qtable.env.",
        "qtable.schema.",
        "qtable.table.describe",
    )

    def select_candidate_tools(
        self,
        manifests: list[SkillManifestEntry],
        request: ToolRouterRequest,
    ) -> list[SkillManifestEntry]:
        allowed = set(request.allowed_skills)
        denied = set(request.denied_skills)
        query_tokens = _tokenize_for_match(request.message)
        query_tokens.update(_tokenize_for_match(" ".join(request.tool_context.notes)))
        query_tokens.update(_tokenize_for_match(" ".join(request.table_ids)))

        ranked: list[tuple[int, SkillManifestEntry]] = []
        for manifest in manifests:
            metadata = manifest.metadata
            if allowed and metadata.name not in allowed:
                continue
            if metadata.name in denied:
                continue

            text_parts = [
                metadata.name,
                metadata.title,
                metadata.description,
                " ".join(metadata.tags),
                " ".join((manifest.input_schema or {}).get("properties", {}).keys()),
            ]
            skill_tokens = _tokenize_for_match(" ".join(text_parts))
            overlap = len(query_tokens & skill_tokens)
            score = overlap * 10

            if "query" in metadata.name or "describe" in metadata.name:
                score += 2
            if "create" in metadata.name and any(token in request.message for token in ["创建", "新增", "添加"]):
                score += 6
            if any(token in request.message for token in ["查看", "查询", "延期", "任务", "统计"]):
                score += 3 if metadata.side_effect == SkillSideEffect.NONE else 0
            if request.table_ids and any(
                permission.resource == "table" for permission in metadata.permissions
            ):
                score += 4

            # 环境感知和 Schema 工具是分析推理的基础，始终给予高优先级
            if metadata.name.startswith(self._CRITICAL_UTILITY_TOOL_PREFIXES):
                score += 20

            ranked.append((score, manifest))

        ranked.sort(key=lambda item: (-item[0], item[1].metadata.name))
        if not ranked:
            return []

        top = [manifest for _, manifest in ranked[: request.tool_limit]]
        if all(score <= 0 for score, _ in ranked):
            return [manifest for _, manifest in ranked[: min(len(ranked), request.tool_limit)]]
        return top

    def build_context_prompt(self, request: ToolRouterRequest) -> str:
        tool_results = request.tool_context.tool_results[-6:]
        public_variables = {
            key: value
            for key, value in request.tool_context.variables.items()
            if not str(key).startswith("_")
        }
        truncated_results: list[dict[str, Any]] = []
        for item in tool_results:
            raw = _safe_json_dumps(item)
            if len(raw) > settings.TOOL_ROUTER_MAX_TOOL_RESULT_CHARS:
                raw = raw[: settings.TOOL_ROUTER_MAX_TOOL_RESULT_CHARS] + "...(truncated)"
            truncated_results.append({"summary": item.get("summary"), "payload": raw})
        context_summary = request.tool_context.context_summary or {}
        context_summary_text = context_summary.get("text") or "无额外业务上下文摘要。"

        # 注入当前真实日期时间作为基准参考
        from datetime import datetime, timezone as dt_timezone
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(request.timezone or "Asia/Shanghai")
        except Exception:
            tz = dt_timezone.utc
        now = datetime.now(tz)
        current_datetime_info = (
            f"当前系统时间: {now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"({now.strftime('%A')}, 时区: {str(tz)}, "
            f"周数: {now.isocalendar()[1]})"
        )

        return (
            "当前执行上下文如下：\n"
            f"- 系统时间: {current_datetime_info}\n"
            f"- workspaceId: {request.workspace_id or request.tool_context.workspace_id or 'unknown'}\n"
            f"- conversationId: {request.conversation_id or request.tool_context.conversation_id or 'unknown'}\n"
            f"- sessionId: {request.session_id or request.tool_context.session_id or 'unknown'}\n"
            f"- projectId: {request.project_id or request.tool_context.project_id or 'unknown'}\n"
            f"- tableIds: {request.table_ids or request.tool_context.table_ids}\n"
            f"- viewId: {request.view_id or request.tool_context.view_id or 'unknown'}\n"
            f"- taskId: {request.task_id or request.tool_context.task_id or 'unknown'}\n"
            f"- teamId: {request.team_id or request.tool_context.team_id or 'unknown'}\n"
            f"- organizationId: {request.organization_id or request.tool_context.organization_id or 'unknown'}\n"
            f"- locale: {request.locale}\n"
            f"- timezone: {request.timezone}\n"
            f"- confirmed: {request.confirmed}\n"
            f"- chainDepth: {request.tool_context.chain_depth}\n"
            f"- contextSummary: {context_summary_text}\n"
            f"- variables: {_safe_json_dumps(public_variables)}\n"
            f"- recentToolResults: {_safe_json_dumps(truncated_results)}\n"
            "如果 recentToolResults 已能回答问题，优先复用，不要重复调工具。"
        )

    def build_history_prompt(self, request: ToolRouterRequest) -> str:
        history_lines: list[str] = []
        for item in request.conversation[-8:]:
            role = item.get("role") or "unknown"
            name = item.get("name")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            prefix = f"[{role}]"
            if name:
                prefix = f"[{role}:{name}]"
            history_lines.append(f"{prefix} {content.strip()}")
        if not history_lines:
            return "无历史对话。"
        return "最近对话：\n" + "\n".join(history_lines)

    def _extract_usage(self, result: Any) -> Optional[RouterUsageInfo]:
        usage = getattr(result, "usage", None)
        if callable(usage):
            usage = usage()
        if not usage:
            return None
        prompt_tokens = getattr(usage, "input_tokens", None)
        completion_tokens = getattr(usage, "output_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if prompt_tokens is None:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
        if completion_tokens is None:
            completion_tokens = getattr(usage, "completion_tokens", None)
        return RouterUsageInfo(
            promptTokens=prompt_tokens,
            completionTokens=completion_tokens,
            totalTokens=total_tokens,
        )

    # 同一工具的最大重复调用次数（防循环）
    MAX_DUPLICATE_CALLS = 3

    def _count_previous_calls(
        self,
        traces: list[ToolExecutionTrace],
        skill_name: str,
        arguments: dict[str, Any],
    ) -> int:
        """统计同一工具+相同关键参数的历史调用次数"""
        count = 0
        key_params = {k: v for k, v in arguments.items() if k in ("tableId", "table_id")}
        for trace in traces:
            if trace.skill_name != skill_name:
                continue
            trace_key = {k: v for k, v in trace.arguments.items() if k in ("tableId", "table_id")}
            if trace_key == key_params:
                count += 1
        return count

    def _append_tool_result_to_context(
        self,
        context: ToolContextState,
        trace: ToolExecutionTrace,
    ) -> ToolContextState:
        tool_results = list(context.tool_results)
        entry = {
            "toolCallId": trace.tool_call_id,
            "skillName": trace.skill_name,
            "state": trace.state,
            "summary": f"{trace.skill_name} -> {trace.state}",
            "output": trace.output,
            "error": trace.error,
        }
        tool_results.append(entry)

        # 防循环：如果同一工具被重复调用超过阈值，在 context 中注入警告
        notes = list(context.notes)
        prev_count = sum(
            1 for r in tool_results
            if r.get("skillName") == trace.skill_name
            and r.get("state") == "completed"
        )
        if prev_count >= self.MAX_DUPLICATE_CALLS and all(
            "防循环警告" not in n for n in notes
        ):
            notes.insert(0, (
                f"防循环警告：工具 {trace.skill_name} 已被调用 {prev_count} 次。"
                f"请立即停止调用该工具，基于已有的返回结果直接给出最终答案。"
                f"不要再使用该工具了！使用不同的工具或直接回答用户。"
            ))

        return ToolContextState.model_validate(
            {
                **context.model_dump(mode="json", by_alias=True),
                "toolResults": tool_results[-10:],
                "chainDepth": context.chain_depth + 1,
                "pendingConfirmation": context.pending_confirmation,
                "notes": notes,
            }
        )

    def _build_tool_runtime_context(
        self,
        request: ToolRouterRequest,
        runtime_mode: str,
        trace_id: str,
        normalized_tool_context: ToolContextState,
        agent_context: AgentContext | None = None,
    ) -> dict[str, Any]:
        return {
            "traceId": trace_id,
            "runtimeMode": runtime_mode,
            "workspaceId": normalized_tool_context.workspace_id,
            "tableIds": normalized_tool_context.table_ids,
            "sessionId": normalized_tool_context.session_id,
            "conversationId": normalized_tool_context.conversation_id,
            "projectId": normalized_tool_context.project_id,
            "viewId": normalized_tool_context.view_id,
            "taskId": normalized_tool_context.task_id,
            "teamId": normalized_tool_context.team_id,
            "organizationId": normalized_tool_context.organization_id,
            "workflow": normalized_tool_context.workflow,
            "agentStack": normalized_tool_context.agent_stack,
            "contextSummary": normalized_tool_context.context_summary,
            "chainDepth": normalized_tool_context.chain_depth,
            "allowedSkills": request.allowed_skills,
            "deniedSkills": request.denied_skills,
            "maxSteps": request.max_steps,
            "toolLimit": request.tool_limit,
            "agentContext": agent_context.model_dump(mode="json", by_alias=True) if agent_context else {},
        }

    def _build_tool_executor(self, runtime: SkillRuntime) -> ToolExecutor:
        metrics_sink = InMemoryToolMetricsSink()
        return ToolExecutor(
            runtime,
            middlewares=[
                LoggingToolMiddleware(logger),
                MetricsToolMiddleware(metrics_sink),
            ],
            hooks=[
                LoggingLifecycleHook(logger),
                MetricsLifecycleHook(metrics_sink),
            ],
        )

    async def _invoke_tool_spec(
        self,
        ctx: RunContext[AgentRuntimeDeps],
        spec,
        payload: BaseModel,
    ) -> dict[str, Any]:
        arguments = payload.model_dump(mode="json", by_alias=True)
        runtime_arguments = payload.model_dump(mode="json", by_alias=True)

        # 防循环检查：如果同一工具+参数已被调用超过阈值，直接拒绝
        dup_count = self._count_previous_calls(ctx.deps.traces, spec.skill_name, arguments)
        if dup_count >= self.MAX_DUPLICATE_CALLS:
            raise ModelRetry(
                f"Tool `{spec.function_name}` has already been called {dup_count} times with "
                f"the same parameters. STOP calling this tool immediately. "
                f"You already have the data you need. "
                f"Based on the previous results, answer the user's question directly. "
                f"Do NOT make another tool call. Return final output with the answer."
            )

        tool_call_id = str(uuid.uuid4())
        adapter_context = build_tool_adapter_context(
            execution_context=ctx.deps.execution_context,
            skill_name=spec.skill_name,
            function_name=spec.function_name,
            runtime_context=ctx.deps.tool_runtime_context,
            request_context={
                "arguments": arguments,
                "confirmed": ctx.deps.confirmed,
            },
            state={
                "metadata": spec.manifest.metadata.model_dump(mode="json"),
                "schemas": spec.schema_bundle.model_dump(mode="json", by_alias=True),
            },
            agent_context=ctx.deps.agent_context.model_dump(mode="json", by_alias=True)
            if ctx.deps.agent_context
            else None,
        )
        outcome = await ctx.deps.tool_executor.execute(
            request=ToolExecutionRequest(
                toolCallId=tool_call_id,
                skillName=spec.skill_name,
                functionName=spec.function_name,
                arguments=runtime_arguments,
                confirmed=ctx.deps.confirmed,
                dry_run=False,
                traceId=ctx.deps.trace_id,
            ),
            execution_context=ctx.deps.execution_context,
            adapter_context=adapter_context,
            retry_config=ToolRetryConfig.model_validate(
                ctx.deps.retry_policy.model_dump(mode="json", by_alias=True)
            ),
        )
        tool_response = outcome.response
        trace = ToolExecutionTrace(
            toolCallId=tool_call_id,
            skillName=spec.skill_name,
            functionName=spec.function_name,
            arguments=arguments,
            attemptCount=outcome.attempt_count,
            state=tool_response.state.value,
            output=tool_response.output,
            error=tool_response.error.model_dump(mode="json") if tool_response.error else None,
            metadata={
                **tool_response.metadata,
                "durationMs": round(outcome.duration_ms, 3),
                "toolSchema": spec.schema_bundle.pydantic_ai_schema,
            },
        )
        ctx.deps.traces.append(trace)
        ctx.deps.tool_context = self._append_tool_result_to_context(ctx.deps.tool_context, trace)

        if tool_response.state.value == "requires_confirmation":
            pending_confirmation = {
                "toolCallId": tool_call_id,
                "skillName": spec.skill_name,
                "functionName": spec.function_name,
                "arguments": arguments,
                "preview": tool_response.metadata,
            }
            ctx.deps.tool_context = ToolContextState.model_validate(
                {
                    **ctx.deps.tool_context.model_dump(mode="json", by_alias=True),
                    "pendingConfirmation": pending_confirmation,
                }
            )
            raise ModelRetry(
                f"Tool `{spec.function_name}` requires confirmation. "
                "Stop calling more tools and return final output with "
                "status='requires_confirmation'."
            )

        if tool_response.error:
            if tool_response.error.code in {
                SkillErrorCode.INVALID_INPUT,
                SkillErrorCode.EXECUTION_FAILED,
            } or tool_response.error.retryable:
                raise ModelRetry(
                    f"Tool `{spec.function_name}` failed and may be recoverable: "
                    f"code={tool_response.error.code.value}, message={tool_response.error.message}, "
                    f"details={tool_response.error.details or {}}. "
                    "Please fix the arguments or choose a better tool."
                )
            return {
                "ok": False,
                "state": tool_response.state.value,
                "output": tool_response.output,
                "error": tool_response.error.model_dump(mode="json"),
                "metadata": tool_response.metadata,
            }

        return {
            "ok": True,
            "state": tool_response.state.value,
            "output": tool_response.output,
            "error": None,
            "metadata": tool_response.metadata,
        }

    def _build_agent(
        self,
        *,
        model: OpenAIChatModel,
        request: ToolRouterRequest,
        tool_adapter_registry: ToolAdapterRegistry,
        runtime_mode: str,
        extra_instructions: str | None,
    ) -> Agent[AgentRuntimeDeps, AgentStructuredOutput]:
        model_name = model.model_name
        is_v4 = is_deepseek_v4_model(model_name)

        # V4 模型在非思考模式下标准 tool_choice='auto' 正常工作。
        # DeepSeekProvider 的 profile 已设置：
        #   openai_supports_tool_choice_required=False
        # 因此 pydantic_ai 发送的是 tool_choice='auto'，V4 原生支持。
        model_settings = OpenAIChatModelSettings(temperature=0.2)

        # V4 模型输出稳定性不如 chat 模型，需要更多输出重试。
        # 避免 "Exceeded maximum output retries (1)" 错误
        output_retries = 5 if is_v4 else 1
        tool_retries = max(1, min(request.tool_context.retry_policy.max_attempts, 2))
        logger.debug(
            "_build_agent: model=%s, is_v4=%s, output_retries=%d",
            model_name, is_v4, output_retries,
        )

        agent = Agent(
            model=model,
            deps_type=AgentRuntimeDeps,
            output_type=AgentStructuredOutput,
            system_prompt=ROUTER_SYSTEM_PROMPT,
            model_settings=model_settings,
            retries={"output": output_retries, "tools": tool_retries},
            tools=tool_adapter_registry.build_pydantic_ai_tools(
                run_context_annotation=RunContext[AgentRuntimeDeps],
                invoke_handler=self._invoke_tool_spec,
            ),
        )

        @agent.instructions
        def runtime_instructions(_ctx: RunContext[AgentRuntimeDeps]) -> str:
            parts = [
                self.build_context_prompt(request),
                self.build_history_prompt(request),
                f"运行模式: {runtime_mode}",
            ]
            if extra_instructions:
                parts.append(extra_instructions)
            return "\n\n".join(parts)

        return agent

    async def _run_agent(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: ToolRouterRequest,
        agent_context: AgentContext | None = None,
        model_override: str | None = None,
        runtime_mode: str = "router",
        extra_instructions: str | None = None,
    ) -> ToolRouterResponse:
        ai_config = await self.load_user_ai_config(db, user_id)
        model, provider = await self.build_provider_model(
            ai_config,
            model_override=model_override or request.model,
        )
        trace_id = str(uuid.uuid4())
        workspace_id = request.workspace_id or request.tool_context.workspace_id
        logger.info(
            "tool_router run start trace_id=%s user_id=%s provider=%s model=%s workspace_id=%s mode=%s",
            trace_id,
            user_id,
            provider.provider,
            provider.model,
            workspace_id,
            runtime_mode,
        )

        runtime_registry = await skill_registry_service.build_runtime_registry(
            db,
            workspace_id=workspace_id,
        )
        all_manifests = runtime_registry.list_manifest()
        candidate_manifests = self.select_candidate_tools(all_manifests, request)
        if not candidate_manifests:
            logger.warning(
                "tool_router no_skills trace_id=%s workspace_id=%s message=%s",
                trace_id,
                workspace_id,
                request.message[:200],
            )
            return ToolRouterResponse(
                status="failed",
                answer="当前没有可用的 Skill 可供路由，请先注册或启用相关 Skill。",
                provider=provider.provider,
                model=provider.model,
                traceId=trace_id,
                steps=0,
                toolCalls=[],
                toolContext=request.tool_context,
                error={"code": "NO_SKILLS_AVAILABLE", "message": "No skills available"},
            )

        runtime = SkillRuntime(runtime_registry)
        tool_adapter_registry = ToolAdapterRegistry.from_runtime_registry(
            runtime_registry,
            skill_names=[manifest.metadata.name for manifest in candidate_manifests],
        )
        tool_executor = self._build_tool_executor(runtime)
        execution_context = SkillExecutionContext(
            context=SkillContext(
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=request.session_id,
                conversation_id=request.conversation_id,
                project_id=request.project_id,
                table_ids=request.table_ids,
                view_id=request.view_id,
                task_id=request.task_id,
                team_id=request.team_id,
                organization_id=request.organization_id,
                workflow_id=request.workflow_id,
                agent_id=request.agent_id,
                trace_id=trace_id,
                origin="agent",
                locale=request.locale,
                timezone=request.timezone,
                dry_run=False,
                confirmed=request.confirmed,
            ),
            db=db,
        )

        normalized_tool_context = ToolContextState.model_validate(
            {
                **request.tool_context.model_dump(mode="json", by_alias=True),
                "workspaceId": workspace_id,
                "tableIds": request.table_ids or request.tool_context.table_ids,
            }
        )
        runtime_context = self._build_tool_runtime_context(
            request,
            runtime_mode,
            trace_id,
            normalized_tool_context,
            agent_context,
        )

        agent = self._build_agent(
            model=model,
            request=request,
            tool_adapter_registry=tool_adapter_registry,
            runtime_mode=runtime_mode,
            extra_instructions=extra_instructions,
        )
        deps = AgentRuntimeDeps(
            tool_executor=tool_executor,
            tool_adapter_registry=tool_adapter_registry,
            execution_context=execution_context,
            tool_context=normalized_tool_context,
            tool_runtime_context=runtime_context,
            retry_policy=normalized_tool_context.retry_policy,
            confirmed=request.confirmed,
            trace_id=trace_id,
            agent_context=agent_context,
        )

        try:
            result = await agent.run(request.message, deps=deps)
            structured = result.output
            usage = self._extract_usage(result)
        except Exception as exc:
            exc_msg = str(exc) or "Agent runtime failed"
            logger.exception(
                "tool_router run failed trace_id=%s workspace_id=%s mode=%s error=%s",
                trace_id,
                workspace_id,
                runtime_mode,
                exc_msg,
            )

            # Check if the error is due to the model not supporting tool calling
            if self.is_tool_calling_unsupported_error(exc_msg):
                friendly_msg = (
                    f"当前模型 {provider.model} 不支持工具调用（如 Agent 模式下的 Skill 路由）。"
                    f"请切换到支持 Function Calling 的模型，例如 deepseek-chat。"
                )
                error_response = ToolRouterResponse(
                    status="failed",
                    answer=friendly_msg,
                    provider=provider.provider,
                    model=provider.model,
                    traceId=trace_id,
                    steps=max(1, len(deps.traces)),
                    toolCalls=deps.traces,
                    toolContext=deps.tool_context,
                    pendingConfirmation=deps.tool_context.pending_confirmation,
                    error={
                        "code": "MODEL_TOOL_CALLING_UNSUPPORTED",
                        "message": exc_msg,
                        "suggestion": "请切换到支持工具调用的模型，例如 deepseek-chat",
                    },
                )
                log_chat_conversation(
                    conversation_id=request.conversation_id or "unknown",
                    user_id=user_id,
                    mode=runtime_mode,
                    user_question=request.message,
                    table_ids=request.table_ids or [],
                    model=provider.model,
                    provider=provider.provider,
                    workspace_id=workspace_id,
                    answer=friendly_msg,
                    status="failed",
                    trace_id=trace_id,
                    tool_calls=[t.model_dump(mode="json", by_alias=True) for t in deps.traces],
                    error={
                        "code": "MODEL_TOOL_CALLING_UNSUPPORTED",
                        "message": exc_msg,
                    },
                )
                return error_response

            # 结构化输出重试次数耗尽（常见于 V4 模型输出格式不稳定）
            if "exceeded maximum output retries" in exc_msg.lower():
                answer = (
                    f"模型 {provider.model} 多次尝试后仍未能生成有效的结构化输出。"
                    f"已完成 {len(deps.traces)} 个工具调用，但最终响应格式不符合预期。"
                    f"建议重试或切换到 deepseek-chat 模型。"
                )
                error_response = ToolRouterResponse(
                    status="failed",
                    answer=answer,
                    provider=provider.provider,
                    model=provider.model,
                    traceId=trace_id,
                    steps=max(1, len(deps.traces)),
                    toolCalls=deps.traces,
                    toolContext=deps.tool_context,
                    pendingConfirmation=deps.tool_context.pending_confirmation,
                    error={
                        "code": "OUTPUT_RETRIES_EXHAUSTED",
                        "message": exc_msg,
                        "suggestion": "建议重试或切换到 deepseek-chat 模型",
                    },
                )
                log_chat_conversation(
                    conversation_id=request.conversation_id or "unknown",
                    user_id=user_id,
                    mode=runtime_mode,
                    user_question=request.message,
                    table_ids=request.table_ids or [],
                    model=provider.model,
                    provider=provider.provider,
                    workspace_id=workspace_id,
                    answer=answer,
                    status="failed",
                    trace_id=trace_id,
                    tool_calls=[t.model_dump(mode="json", by_alias=True) for t in deps.traces],
                    error={"code": "OUTPUT_RETRIES_EXHAUSTED", "message": exc_msg},
                )
                return error_response

            error_response = ToolRouterResponse(
                status="failed",
                answer=exc_msg,
                provider=provider.provider,
                model=provider.model,
                traceId=trace_id,
                steps=max(1, len(deps.traces)),
                toolCalls=deps.traces,
                toolContext=deps.tool_context,
                pendingConfirmation=deps.tool_context.pending_confirmation,
                error={"code": "AGENT_RUNTIME_FAILED", "message": exc_msg},
            )

            # 记录失败的 AI 对话日志
            log_chat_conversation(
                conversation_id=request.conversation_id or "unknown",
                user_id=user_id,
                mode=runtime_mode,
                user_question=request.message,
                table_ids=request.table_ids or [],
                model=provider.model,
                provider=provider.provider,
                workspace_id=workspace_id,
                answer=str(exc) or "Agent runtime failed",
                status="failed",
                trace_id=trace_id,
                tool_calls=[t.model_dump(mode="json", by_alias=True) for t in deps.traces],
                error={"code": "AGENT_RUNTIME_FAILED", "message": str(exc) or "Agent runtime failed"},
            )

            return error_response

        status = structured.status
        answer = structured.answer.strip() or "已完成工具执行，但模型没有返回最终文本结果。"
        if deps.tool_context.pending_confirmation:
            status = "requires_confirmation"
            if not answer:
                answer = "检测到需要确认的操作，请确认后继续执行。"
        logger.info(
            "tool_router run finish trace_id=%s status=%s steps=%s tool_calls=%s",
            trace_id,
            status,
            max(1, len(deps.traces)),
            len(deps.traces),
        )

        response = ToolRouterResponse(
            status=status,
            answer=answer,
            provider=provider.provider,
            model=provider.model,
            traceId=trace_id,
            steps=max(1, len(deps.traces)),
            toolCalls=deps.traces,
            toolContext=deps.tool_context,
            usage=usage,
            pendingConfirmation=deps.tool_context.pending_confirmation,
        )

        # 记录完整的 AI 对话日志
        log_chat_conversation(
            conversation_id=request.conversation_id or "unknown",
            user_id=user_id,
            mode=runtime_mode,
            user_question=request.message,
            table_ids=request.table_ids or [],
            model=provider.model,
            provider=provider.provider,
            workspace_id=workspace_id,
            answer=answer,
            status=status,
            trace_id=trace_id,
            tool_calls=[t.model_dump(mode="json", by_alias=True) for t in deps.traces],
        )

        return response

    async def run(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: ToolRouterRequest,
        agent_context: AgentContext | None = None,
        model_override: str | None = None,
    ) -> ToolRouterResponse:
        return await self._run_agent(
            db=db,
            user_id=user_id,
            request=request,
            agent_context=agent_context,
            model_override=model_override,
            runtime_mode="router",
        )

    async def preview_record_creation(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        user_request: str,
        table_ids: list[str],
        workspace_id: str | None = None,
        conversation_id: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        view_id: str | None = None,
        task_id: str | None = None,
        team_id: str | None = None,
        organization_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        agent_context: AgentContext | None = None,
        model_override: str | None = None,
        locale: str = "zh-CN",
        timezone: str = "Asia/Shanghai",
    ) -> ToolRouterResponse:
        request = ToolRouterRequest(
            message=user_request,
            model=model_override,
            sessionId=session_id,
            conversationId=conversation_id,
            workspaceId=workspace_id,
            projectId=project_id,
            tableIds=table_ids,
            viewId=view_id,
            taskId=task_id,
            teamId=team_id,
            organizationId=organization_id,
            workflowId=workflow_id,
            agentId=agent_id,
            toolContext=ToolContextState(
                sessionId=session_id,
                conversationId=conversation_id,
                workspaceId=workspace_id,
                projectId=project_id,
                tableIds=table_ids,
                viewId=view_id,
                taskId=task_id,
                teamId=team_id,
                organizationId=organization_id,
                workflow={"id": workflow_id} if workflow_id else {},
                notes=[
                    "当前任务是根据用户需求生成待确认的新记录草稿。",
                    "必须先调用 qtable.table.describe 获取所有字段的 id、name、type、options 信息。",
                    "创建记录时，values 字典的 key 必须是字段的 id（如 fld_xxxxx），严禁使用字段的 name。",
                    "select/multiSelect 字段的值必须是选项的 id（如 opt1），不允许使用 label。",
                ],
            ),
            allowedSkills=["qtable.table.describe", "qtable.record.create"],
            maxSteps=4,
            toolLimit=4,
            confirmed=False,
            locale=locale,
            timezone=timezone,
        )
        return await self._run_agent(
            db=db,
            user_id=user_id,
            request=request,
            agent_context=agent_context,
            model_override=model_override,
            runtime_mode="record_create_preview",
            extra_instructions=RECORD_CREATE_SYSTEM_APPEND,
        )


tool_router_service = ToolRouterService()
