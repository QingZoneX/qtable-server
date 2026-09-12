from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.deepseek_helper import (
    DEEPSEEK_BASE_URL,
    build_deepseek_model,
    create_deepseek_client,
)
from app.models.ai_config import AiConfig
from app.schemas.structured_output import (
    StructuredConversationMessage,
    StructuredEvidence,
    StructuredOutputRequest,
    StructuredOutputResponse,
    StructuredOutputResult,
    StructuredOutputStreamEvent,
    ToolObservation,
    ValidationIssue,
    ValidationReport,
)
from app.services.ai_tool_router import tool_router_service
from app.services.encryption import decrypt_api_key
from app.services.smart_table_store import get_full_store, get_full_store_for_table
from app.services.row_permissions import filter_store_for_user
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)

logger = logging.getLogger(__name__)


@dataclass
class StructuredProviderHandle:
    provider: str
    model_name: str
    base_url: str
    client: AsyncOpenAI
    agent_model: OpenAIChatModel


@dataclass
class StructuredOutputDeps:
    db: AsyncSession
    user_id: int
    request: StructuredOutputRequest
    trace_id: str
    tool_observations: list[ToolObservation] = field(default_factory=list)
    event_queue: asyncio.Queue[StructuredOutputStreamEvent] | None = None


class TableContextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    sample_limit: int = Field(default=8, ge=1, le=20, alias="sampleLimit")


class TableFieldSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: str
    options: list[dict[str, Any]] = Field(default_factory=list)


class TableContextSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(alias="tableId")
    field_count: int = Field(alias="fieldCount")
    record_count: int = Field(alias="recordCount")
    fields: list[TableFieldSummary] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list, alias="sampleRows")


class TableContextResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableContextSummary] = Field(default_factory=list)


class PreviewRecordDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_request: str = Field(alias="userRequest")
    table_id: Optional[str] = Field(default=None, alias="tableId")


class PreviewRecordDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    message: str
    table_id: Optional[str] = Field(default=None, alias="tableId")
    values: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = Field(default=False, alias="requiresConfirmation")
    tool_trace_id: Optional[str] = Field(default=None, alias="toolTraceId")


class StructuredOutputService:
    def _build_provider_handle(
        self,
        config: AiConfig,
        *,
        model_override: str | None = None,
    ) -> StructuredProviderHandle:
        provider = (config.provider or "deepseek").strip().lower()
        api_key = decrypt_api_key(config.api_key_encrypted)
        if provider == "openai":
            base_url = (settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            model_name = model_override or config.model or settings.OPENAI_MODEL or "gpt-4o-mini"
            provider_name = "openai"
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
            agent_model = OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(openai_client=client),
            )
        elif provider in {"deepseek", "deepseek-chat", "deepseek-reasoner"}:
            model_name = model_override or config.model or settings.DEEPSEEK_MODEL or "deepseek-chat"
            base_url = (settings.DEEPSEEK_BASE_URL or DEEPSEEK_BASE_URL).rstrip("/")
            provider_name = "deepseek"
            client = create_deepseek_client(api_key, model_name, base_url)
            agent_model = build_deepseek_model(api_key, model_name, base_url, openai_client=client)
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
            provider_name = "openai-compatible"
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
            agent_model = OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(openai_client=client),
            )

        return StructuredProviderHandle(
            provider=provider_name,
            model_name=model_name,
            base_url=base_url,
            client=client,
            agent_model=agent_model,
        )

    def _build_history_block(self, history: list[StructuredConversationMessage]) -> str:
        if not history:
            return "无历史对话。"
        lines: list[str] = []
        for item in history[-8:]:
            name = f":{item.name}" if item.name else ""
            lines.append(f"[{item.role}{name}] {item.content.strip()}")
        return "\n".join(lines)

    def _build_runtime_context(self, request: StructuredOutputRequest) -> str:
        return (
            f"- mode: {request.mode}\n"
            f"- workspaceId: {request.workspace_id or 'unknown'}\n"
            f"- sessionId: {request.session_id or 'unknown'}\n"
            f"- conversationId: {request.conversation_id or 'unknown'}\n"
            f"- projectId: {request.project_id or 'unknown'}\n"
            f"- tableIds: {request.table_ids}\n"
            f"- viewId: {request.view_id or 'unknown'}\n"
            f"- taskId: {request.task_id or 'unknown'}\n"
            f"- teamId: {request.team_id or 'unknown'}\n"
            f"- organizationId: {request.organization_id or 'unknown'}\n"
            f"- workflowId: {request.workflow_id or 'unknown'}\n"
            f"- agentId: {request.agent_id or 'unknown'}\n"
            f"- locale: {request.locale}\n"
            f"- timezone: {request.timezone}\n"
            f"- useTools: {request.use_tools}"
        )

    def _build_schema_instruction(self) -> str:
        schema = StructuredOutputResult.model_json_schema()
        return json.dumps(schema, ensure_ascii=False, indent=2)

    def _build_system_prompt(self, request: StructuredOutputRequest) -> str:
        mode_rules = {
            "analysis": "优先输出结构化分析结论、指标、证据和可执行建议。",
            "record_draft": "必须优先生成 recordDraft；如果存在写入风险，状态使用 requires_confirmation。",
            "workflow": "优先输出可执行步骤、前置依赖、风险和下一步行动。",
        }
        return (
            "你是 QTable 的企业级 Structured Output Agent。\n\n"
            "你的目标：输出稳定、结构化、可验证、可执行的 JSON 结果。\n\n"
            "硬性要求：\n"
            "1. 最终输出必须严格符合给定 JSON Schema。\n"
            "2. 结论必须尽量基于真实上下文和工具结果，禁止编造。\n"
            "3. 如果使用工具，evidence.sourceType 优先标记为 tool 或 table。\n"
            "4. 如果信息不足，应在 warnings 和 nextSteps 中明确说明，而不是伪造数据。\n"
            "5. 如果是记录草稿场景，recordDraft 中应给出字段值和确认状态。\n"
            "6. 不要返回 markdown，不要返回解释性前缀，只返回结构化结果。\n\n"
            f"当前模式要求：{mode_rules.get(request.mode, mode_rules['analysis'])}\n\n"
            "目标输出 Schema：\n"
            f"{self._build_schema_instruction()}"
        )

    def _build_user_prompt(self, request: StructuredOutputRequest) -> str:
        return (
            "请基于以下上下文生成结构化结果。\n\n"
            "运行上下文：\n"
            f"{self._build_runtime_context(request)}\n\n"
            "最近对话：\n"
            f"{self._build_history_block(request.conversation)}\n\n"
            "用户请求：\n"
            f"{request.prompt}"
        )

    async def _emit_event(
        self,
        deps: StructuredOutputDeps,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        if deps.event_queue is None:
            return
        await deps.event_queue.put(
            StructuredOutputStreamEvent(type=event_type, traceId=deps.trace_id, data=data)
        )

    async def _record_tool_observation(
        self,
        deps: StructuredOutputDeps,
        *,
        tool_name: str,
        state: str,
        attempt: int,
        latency_ms: float,
        arguments: dict[str, Any],
        result: dict[str, Any] | None,
        input_schema: dict[str, Any],
        result_schema: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        observation = ToolObservation(
            toolName=tool_name,
            state=state,
            attempt=attempt,
            latencyMs=round(latency_ms, 3),
            arguments=arguments,
            result=result,
            inputSchema=input_schema,
            resultSchema=result_schema,
            error=error,
        )
        deps.tool_observations.append(observation)
        await self._emit_event(
            deps,
            "tool",
            observation.model_dump(mode="json", by_alias=True),
        )

    async def _load_table_context(
        self,
        db: AsyncSession,
        *,
        table_ids: list[str],
        sample_limit: int,
        user_id: int,
    ) -> TableContextResult:
        backend = (settings.DATA_BACKEND or "").lower()
        tables: list[TableContextSummary] = []
        effective_table_ids = list(table_ids)
        if not effective_table_ids:
            if backend in {"db", "database", "postgres", "sqlite"}:
                return TableContextResult(tables=[])
            effective_table_ids = ["dstDefault"]
        for table_id in effective_table_ids:
            if backend in {"db", "database", "postgres", "sqlite"}:
                permission = await get_effective_permission_for_item(
                    db,
                    user_id,
                    table_id,
                )
                if not permission_allows(permission, "read"):
                    raise PermissionError("No access to target table")
                store = await get_full_store(db, table_id)
                store, _ = await filter_store_for_user(
                    db,
                    table_id,
                    store,
                    user_id=user_id,
                    table_permission=permission,
                )
            else:
                store = await get_full_store_for_table(
                    table_id if table_id != "dstDefault" else None
                )
            fields = store.get("fields", [])
            records = store.get("records", [])
            table_fields = [
                TableFieldSummary(
                    id=str(field.get("id", "")),
                    name=str(field.get("name", field.get("id", ""))),
                    type=str(field.get("type", "text")),
                    options=field.get("options") or [],
                )
                for field in fields
                if field.get("id")
            ]
            sample_rows = records[:sample_limit]
            tables.append(
                TableContextSummary(
                    tableId=table_id,
                    fieldCount=len(table_fields),
                    recordCount=len(records),
                    fields=table_fields,
                    sampleRows=sample_rows,
                )
            )
        return TableContextResult(tables=tables)

    def _extract_record_values(self, preview_response: Any) -> dict[str, Any]:
        pending = getattr(preview_response, "pending_confirmation", None) or {}
        arguments = pending.get("arguments") if isinstance(pending, dict) else None
        values = arguments.get("values") if isinstance(arguments, dict) else None
        return values if isinstance(values, dict) else {}

    def _validate_business_rules(
        self,
        request: StructuredOutputRequest,
        result: StructuredOutputResult,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if request.mode == "record_draft" and result.record_draft is None:
            issues.append(
                ValidationIssue(
                    code="RECORD_DRAFT_REQUIRED",
                    message="record_draft mode requires recordDraft in the final output",
                    field="recordDraft",
                )
            )
        if result.status == "requires_confirmation" and result.record_draft is None:
            issues.append(
                ValidationIssue(
                    code="CONFIRMATION_REQUIRES_DRAFT",
                    message="requires_confirmation output must include recordDraft",
                    field="recordDraft",
                )
            )
        if request.mode == "analysis" and not result.sections:
            issues.append(
                ValidationIssue(
                    code="ANALYSIS_SECTIONS_EMPTY",
                    message="analysis mode should return at least one structured section",
                    field="sections",
                    severity="warning",
                )
            )
        return issues

    def _validation_report(
        self,
        *,
        request: StructuredOutputRequest,
        result: StructuredOutputResult,
        repaired: bool,
        recovery_stage: str,
    ) -> ValidationReport:
        issues = self._validate_business_rules(request, result)
        passed = not any(issue.severity == "error" for issue in issues)
        return ValidationReport(
            passed=passed,
            repaired=repaired,
            recoveryStage=recovery_stage,
            issues=issues,
        )

    def _build_agent(
        self,
        provider: StructuredProviderHandle,
        request: StructuredOutputRequest,
    ) -> Agent[StructuredOutputDeps, StructuredOutputResult]:
        agent = Agent(
            model=provider.agent_model,
            deps_type=StructuredOutputDeps,
            output_type=StructuredOutputResult,
            system_prompt=self._build_system_prompt(request),
            model_settings=OpenAIChatModelSettings(temperature=0.1),
            output_retries=request.retry.max_output_retries,
            tool_retries=request.retry.max_tool_retries,
        )

        @agent.instructions
        def runtime_instructions(_ctx: RunContext[StructuredOutputDeps]) -> str:
            return (
                "补充要求：\n"
                "- summary 和 answer 必须可直接给业务方消费。\n"
                "- 如果使用了工具，尽量把关键依据写入 evidence。\n"
                "- warnings 用于表达缺口、风险、假设。\n"
                "- nextSteps 用于给出闭环动作。\n"
            )

        @agent.output_validator
        def validate_output(
            ctx: RunContext[StructuredOutputDeps],
            output: StructuredOutputResult,
        ) -> StructuredOutputResult:
            issues = self._validate_business_rules(ctx.deps.request, output)
            errors = [issue.message for issue in issues if issue.severity == "error"]
            if errors:
                raise ModelRetry("; ".join(errors))
            return output

        if request.use_tools:

            @agent.tool(
                name="load_table_context",
                retries=request.retry.max_tool_retries,
                include_return_schema=True,
            )
            async def load_table_context(
                ctx: RunContext[StructuredOutputDeps],
                payload: TableContextArgs,
            ) -> TableContextResult:
                started = time.perf_counter()
                arguments = payload.model_dump(mode="json", by_alias=True)
                try:
                    result = await self._load_table_context(
                        ctx.deps.db,
                        table_ids=payload.table_ids or ctx.deps.request.table_ids,
                        sample_limit=payload.sample_limit,
                        user_id=ctx.deps.user_id,
                    )
                    await self._record_tool_observation(
                        ctx.deps,
                        tool_name="load_table_context",
                        state="completed",
                        attempt=1,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        arguments=arguments,
                        result=result.model_dump(mode="json", by_alias=True),
                        input_schema=TableContextArgs.model_json_schema(),
                        result_schema=TableContextResult.model_json_schema(),
                    )
                    return result
                except Exception as exc:
                    await self._record_tool_observation(
                        ctx.deps,
                        tool_name="load_table_context",
                        state="failed",
                        attempt=1,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        arguments=arguments,
                        result=None,
                        input_schema=TableContextArgs.model_json_schema(),
                        result_schema=TableContextResult.model_json_schema(),
                        error={"code": "TABLE_CONTEXT_FAILED", "message": str(exc)},
                    )
                    raise

            @agent.tool(
                name="preview_record_draft",
                retries=request.retry.max_tool_retries,
                include_return_schema=True,
            )
            async def preview_record_draft(
                ctx: RunContext[StructuredOutputDeps],
                payload: PreviewRecordDraftArgs,
            ) -> PreviewRecordDraftResult:
                started = time.perf_counter()
                arguments = payload.model_dump(mode="json", by_alias=True)
                table_id = payload.table_id or (ctx.deps.request.table_ids[0] if ctx.deps.request.table_ids else None)
                if not table_id:
                    raise ValueError("tableId is required for preview_record_draft")
                try:
                    preview = await tool_router_service.preview_record_creation(
                        db=ctx.deps.db,
                        user_id=ctx.deps.user_id,
                        user_request=payload.user_request,
                        table_ids=[table_id],
                        workspace_id=ctx.deps.request.workspace_id,
                        conversation_id=ctx.deps.request.conversation_id,
                        session_id=ctx.deps.request.session_id,
                        project_id=ctx.deps.request.project_id,
                        view_id=ctx.deps.request.view_id,
                        task_id=ctx.deps.request.task_id,
                        team_id=ctx.deps.request.team_id,
                        organization_id=ctx.deps.request.organization_id,
                        workflow_id=ctx.deps.request.workflow_id,
                        agent_id=ctx.deps.request.agent_id,
                        locale=ctx.deps.request.locale,
                        timezone=ctx.deps.request.timezone,
                    )
                    result = PreviewRecordDraftResult(
                        status=preview.status,
                        message=preview.answer or "Record draft generated",
                        tableId=table_id,
                        values=self._extract_record_values(preview),
                        requiresConfirmation=bool(preview.pending_confirmation),
                        toolTraceId=preview.trace_id,
                    )
                    await self._record_tool_observation(
                        ctx.deps,
                        tool_name="preview_record_draft",
                        state="completed",
                        attempt=1,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        arguments=arguments,
                        result=result.model_dump(mode="json", by_alias=True),
                        input_schema=PreviewRecordDraftArgs.model_json_schema(),
                        result_schema=PreviewRecordDraftResult.model_json_schema(),
                    )
                    return result
                except Exception as exc:
                    await self._record_tool_observation(
                        ctx.deps,
                        tool_name="preview_record_draft",
                        state="failed",
                        attempt=1,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        arguments=arguments,
                        result=None,
                        input_schema=PreviewRecordDraftArgs.model_json_schema(),
                        result_schema=PreviewRecordDraftResult.model_json_schema(),
                        error={"code": "RECORD_PREVIEW_FAILED", "message": str(exc)},
                    )
                    raise

        return agent

    def _extract_json_payload(self, text: str) -> str:
        cleaned = (text or "").strip().replace("\ufeff", "")
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        start_positions = [pos for pos in (cleaned.find("{"), cleaned.find("[")) if pos >= 0]
        if not start_positions:
            return cleaned
        start = min(start_positions)
        return cleaned[start:].strip()

    def repair_json_text(self, text: str) -> str:
        cleaned = self._extract_json_payload(text)
        cleaned = cleaned.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        cleaned = re.sub(r"//.*?$", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)

        open_braces = cleaned.count("{")
        close_braces = cleaned.count("}")
        if open_braces > close_braces:
            cleaned += "}" * (open_braces - close_braces)
        open_brackets = cleaned.count("[")
        close_brackets = cleaned.count("]")
        if open_brackets > close_brackets:
            cleaned += "]" * (open_brackets - close_brackets)
        return cleaned.strip()

    async def _repair_with_raw_completion(
        self,
        *,
        provider: StructuredProviderHandle,
        request: StructuredOutputRequest,
        deps: StructuredOutputDeps,
        last_error: Exception,
    ) -> StructuredOutputResponse:
        repair_prompt = (
            "请根据以下信息重新输出严格合法的 JSON，不要输出 markdown。\n\n"
            "目标 Schema：\n"
            f"{self._build_schema_instruction()}\n\n"
            "用户请求：\n"
            f"{request.prompt}\n\n"
            "历史对话：\n"
            f"{self._build_history_block(request.conversation)}\n\n"
            "已收集的工具观测：\n"
            f"{json.dumps([item.model_dump(mode='json', by_alias=True) for item in deps.tool_observations], ensure_ascii=False, indent=2)}\n\n"
            "上一次失败原因：\n"
            f"{str(last_error)}"
        )
        response = await provider.client.chat.completions.create(
            model=provider.model_name,
            messages=[
                {"role": "system", "content": self._build_system_prompt(request)},
                {"role": "user", "content": repair_prompt},
            ],
            stream=False,
            temperature=0.0,
        )
        raw_text = response.choices[0].message.content or ""
        repaired = self.repair_json_text(raw_text)
        result = StructuredOutputResult.model_validate_json(repaired)
        validation = self._validation_report(
            request=request,
            result=result,
            repaired=True,
            recovery_stage="repaired_json",
        )
        if not validation.passed:
            raise ValueError("repaired output still violates business rules")
        return StructuredOutputResponse(
            traceId=deps.trace_id,
            provider=provider.provider,
            model=provider.model_name,
            attempts=request.retry.max_attempts + 1,
            result=result,
            validation=validation,
            toolObservations=deps.tool_observations,
            rawResponseText=raw_text,
            error=None,
        )

    async def _run_native_once(
        self,
        *,
        provider: StructuredProviderHandle,
        deps: StructuredOutputDeps,
    ) -> StructuredOutputResponse:
        agent = self._build_agent(provider, deps.request)
        result = await agent.run(
            self._build_user_prompt(deps.request),
            deps=deps,
            output_retries=deps.request.retry.max_output_retries,
        )
        structured = result.output
        validation = self._validation_report(
            request=deps.request,
            result=structured,
            repaired=False,
            recovery_stage="native",
        )
        if not validation.passed:
            raise ValueError("native output failed business validation")
        return StructuredOutputResponse(
            traceId=deps.trace_id,
            provider=provider.provider,
            model=provider.model_name,
            attempts=1,
            result=structured,
            validation=validation,
            toolObservations=deps.tool_observations,
        )

    async def run(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: StructuredOutputRequest,
    ) -> StructuredOutputResponse:
        config = await tool_router_service.load_user_ai_config(db, user_id)
        provider = self._build_provider_handle(config, model_override=request.model)
        trace_id = str(uuid.uuid4())
        last_error: Exception | None = None
        last_observations: list[ToolObservation] = []

        for attempt in range(1, request.retry.max_attempts + 1):
            deps = StructuredOutputDeps(
                db=db,
                user_id=user_id,
                request=request,
                trace_id=trace_id,
            )
            try:
                response = await self._run_native_once(provider=provider, deps=deps)
                response.attempts = attempt
                return response
            except Exception as exc:
                last_error = exc
                last_observations = list(deps.tool_observations)
                logger.warning(
                    "structured_output native attempt failed trace_id=%s attempt=%s error=%s",
                    trace_id,
                    attempt,
                    str(exc),
                )
                if attempt < request.retry.max_attempts and request.retry.backoff_ms:
                    await asyncio.sleep(request.retry.backoff_ms / 1000)

        failed_deps = StructuredOutputDeps(
            db=db,
            user_id=user_id,
            request=request,
            trace_id=trace_id,
            tool_observations=last_observations,
        )
        if request.retry.enable_repair and last_error is not None:
            try:
                return await self._repair_with_raw_completion(
                    provider=provider,
                    request=request,
                    deps=failed_deps,
                    last_error=last_error,
                )
            except Exception as repair_exc:
                last_error = repair_exc
                logger.warning(
                    "structured_output repair failed trace_id=%s error=%s",
                    trace_id,
                    str(repair_exc),
                )

        failed_result = StructuredOutputResult(
            status="failed",
            intent="record_draft" if request.mode == "record_draft" else request.mode,
            summary="Structured output generation failed",
            answer=str(last_error) or "Structured output generation failed",
            sections=[],
            actions=[],
            warnings=["输出验证失败，且修复链路未恢复成功。"],
            nextSteps=["检查模型配置、上下文约束或缩小输出范围后重试。"],
            evidence=[
                StructuredEvidence(
                    sourceType="model",
                    sourceId="structured-output-service",
                    quote=str(last_error) or "unknown error",
                )
            ],
            confidence=0.0,
        )
        return StructuredOutputResponse(
            traceId=trace_id,
            provider=provider.provider,
            model=provider.model_name,
            attempts=request.retry.max_attempts,
            result=failed_result,
            validation=ValidationReport(
                passed=False,
                repaired=False,
                recoveryStage="failed",
                issues=[
                    ValidationIssue(
                        code="STRUCTURED_OUTPUT_FAILED",
                        message=str(last_error) or "Structured output generation failed",
                    )
                ],
            ),
            toolObservations=last_observations,
            error={
                "code": "STRUCTURED_OUTPUT_FAILED",
                "message": str(last_error) or "Structured output generation failed",
            },
        )

    async def stream(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: StructuredOutputRequest,
    ) -> AsyncIterator[StructuredOutputStreamEvent]:
        config = await tool_router_service.load_user_ai_config(db, user_id)
        provider = self._build_provider_handle(config, model_override=request.model)
        trace_id = str(uuid.uuid4())
        queue: asyncio.Queue[StructuredOutputStreamEvent] = asyncio.Queue()
        deps = StructuredOutputDeps(
            db=db,
            user_id=user_id,
            request=request,
            trace_id=trace_id,
            event_queue=queue,
        )
        agent = self._build_agent(provider, request)

        async def producer() -> None:
            try:
                await self._emit_event(
                    deps,
                    "status",
                    {"stage": "started", "provider": provider.provider, "model": provider.model_name},
                )
                async with agent.run_stream(
                    self._build_user_prompt(request),
                    deps=deps,
                    output_retries=request.retry.max_output_retries,
                ) as result:
                    async for partial in result.stream_output(debounce_by=0.05):
                        await self._emit_event(
                            deps,
                            "partial",
                            partial.model_dump(mode="json", by_alias=True),
                        )
                    final_output = await result.get_output()
                    validation = self._validation_report(
                        request=request,
                        result=final_output,
                        repaired=False,
                        recovery_stage="native",
                    )
                    final_response = StructuredOutputResponse(
                        traceId=trace_id,
                        provider=provider.provider,
                        model=provider.model_name,
                        attempts=1,
                        result=final_output,
                        validation=validation,
                        toolObservations=deps.tool_observations,
                    )
                    await self._emit_event(
                        deps,
                        "final",
                        final_response.model_dump(mode="json", by_alias=True),
                    )
            except Exception as exc:
                if request.retry.enable_repair:
                    try:
                        repaired = await self._repair_with_raw_completion(
                            provider=provider,
                            request=request,
                            deps=deps,
                            last_error=exc,
                        )
                        await self._emit_event(
                            deps,
                            "final",
                            repaired.model_dump(mode="json", by_alias=True),
                        )
                        return
                    except Exception as repair_exc:
                        exc = repair_exc
                await self._emit_event(
                    deps,
                    "error",
                    {
                        "code": "STRUCTURED_OUTPUT_STREAM_FAILED",
                        "message": str(exc) or "Structured output streaming failed",
                    },
                )

        task = asyncio.create_task(producer())
        while not task.done() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.1)
                yield event
            except asyncio.TimeoutError:
                continue
        await task


structured_output_service = StructuredOutputService()
