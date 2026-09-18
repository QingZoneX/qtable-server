from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

import redis.asyncio as redis
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.builder import context_builder
from app.context_engine.models import AgentContext, ContextBuildInput
from app.core.config import settings
from app.services.deepseek_helper import (
    DEEPSEEK_BASE_URL,
    build_deepseek_model,
    is_deepseek_v4_model,
)
from app.models.estimate_workload import WorkloadEstimateRun
from app.schemas.estimate_workload import (
    ContextInsight,
    EstimateConfidence,
    EstimateFeedbackRequest,
    EstimateWorkloadRequest,
    EstimateWorkloadResponse,
    EstimateWorkloadResult,
    HistoricalEstimateSignal,
    HistoricalLearningSummary,
    WorkloadBreakdownItem,
    WorkloadRisk,
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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _tokenize(value: str) -> set[str]:
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{1,4}", (value or "").lower())
    return {item for item in parts if item}


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _round_metric(value: float) -> float:
    return round(float(value or 0.0), 2)


class TableContextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    sample_limit: int = Field(default=5, ge=1, le=12, alias="sampleLimit")


class TableFieldSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: str


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


class HistoricalEstimateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=6, ge=1, le=12)


class HistoricalEstimateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(default=0, alias="sampleCount")
    feedback_sample_count: int = Field(default=0, alias="feedbackSampleCount")
    average_hours_per_story_point: float = Field(
        default=0.0,
        alias="averageHoursPerStoryPoint",
    )
    average_bias: float = Field(default=1.0, alias="averageBias")
    examples: list[HistoricalEstimateSignal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@dataclass
class EstimateWorkloadDeps:
    db: AsyncSession
    user_id: int
    request: EstimateWorkloadRequest
    agent_context: AgentContext
    history: HistoricalLearningSummary
    tool_log: list[dict[str, Any]] = field(default_factory=list)


class EstimateWorkloadService:
    HISTORY_CACHE_PREFIX = "estimate-workload-history"

    def __init__(self) -> None:
        self._redis_client: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis | None:
        if self._redis_client is not None:
            return self._redis_client
        try:
            client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.5,
            )
            await client.ping()
            self._redis_client = client
            return client
        except Exception:
            return None

    async def _get_cached_json(self, key: str) -> Any | None:
        client = await self._get_redis()
        if client is None:
            return None
        try:
            raw = await client.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _set_cached_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        client = await self._get_redis()
        if client is None:
            return
        try:
            await client.setex(key, ttl_seconds, json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return

    async def invalidate_history_cache(self, workspace_id: str | None) -> None:
        client = await self._get_redis()
        if client is None:
            return
        pattern = f"{self.HISTORY_CACHE_PREFIX}:{workspace_id or 'global'}:*"
        cursor = 0
        try:
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    await client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            return

    def _build_provider_model(
        self,
        *,
        provider: str,
        api_key: str,
        model_name: str,
        base_url: str,
    ) -> tuple[OpenAIChatModel, str, str]:
        # DeepSeek 系列模型使用 build_deepseek_model 以获得 V4 兼容处理
        if provider == "deepseek":
            model = build_deepseek_model(api_key, model_name, base_url)
            return model, provider, model_name

        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        return (
            OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(openai_client=client),
            ),
            provider,
            model_name,
        )

    async def _load_provider_model(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        model_override: str | None,
    ) -> tuple[OpenAIChatModel, str, str]:
        config = await tool_router_service.load_user_ai_config(db, user_id)
        provider_name = (config.provider or "deepseek").strip().lower()
        api_key = decrypt_api_key(config.api_key_encrypted)
        if provider_name == "openai":
            return self._build_provider_model(
                provider="openai",
                api_key=api_key,
                model_name=model_override or config.model or settings.OPENAI_MODEL or "gpt-4o-mini",
                base_url=(settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/"),
            )
        if provider_name in {"deepseek", "deepseek-chat", "deepseek-reasoner"}:
            model_name = model_override or config.model or settings.DEEPSEEK_MODEL or "deepseek-chat"
            base_url = (settings.DEEPSEEK_BASE_URL or DEEPSEEK_BASE_URL).rstrip("/")
            model = build_deepseek_model(api_key, model_name, base_url)
            return model, "deepseek", model_name
        return self._build_provider_model(
            provider="openai-compatible",
            api_key=api_key,
            model_name=(
                model_override
                or config.model
                or settings.OPENAI_MODEL
                or settings.DEEPSEEK_MODEL
                or "gpt-4o-mini"
            ),
            base_url=(
                settings.OPENAI_BASE_URL
                or settings.DEEPSEEK_BASE_URL
                or "https://api.openai.com/v1"
            ).rstrip("/"),
        )

    async def _build_agent_context(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: EstimateWorkloadRequest,
        trace_id: str,
    ) -> AgentContext:
        return await context_builder.build(
            db,
            ContextBuildInput(
                source="estimate_workload_service",
                userId=user_id,
                sessionId=request.session_id,
                conversationId=request.conversation_id,
                workspaceId=request.workspace_id,
                projectId=request.project_id,
                tableIds=request.table_ids,
                viewId=request.view_id,
                taskId=request.task_id,
                teamId=request.team_id,
                organizationId=request.organization_id,
                workflowId=request.workflow_id,
                agentId=request.agent_id,
                message=request.prompt,
                locale=request.locale,
                timezone=request.timezone,
                metadata={
                    "estimate": {
                        "qualityBar": request.quality_bar,
                        "businessDomain": request.business_domain,
                    }
                },
            ),
            trace_id=trace_id,
        )

    def _history_cache_key(
        self,
        request: EstimateWorkloadRequest,
        agent_context: AgentContext,
        *,
        user_id: int,
    ) -> str:
        raw = json.dumps(
            {
                "workspaceId": request.workspace_id,
                "projectId": request.project_id,
                "teamId": request.team_id,
                "qualityBar": request.quality_bar,
                "prompt": request.prompt,
                "businessDomain": request.business_domain,
                "stack": request.tech_stack.model_dump(mode="json", by_alias=True),
                "tags": request.tags,
                "scopeKey": agent_context.scope_key,
                "userId": user_id,
                "lookbackDays": request.historical_learning.lookback_days,
                "requireFeedback": request.historical_learning.require_feedback,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"{self.HISTORY_CACHE_PREFIX}:{request.workspace_id or 'global'}:{digest}"

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
        for table_id in table_ids[:8]:
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
            tables.append(
                TableContextSummary(
                    tableId=table_id,
                    fieldCount=len(fields),
                    recordCount=len(records),
                    fields=[
                        TableFieldSummary(
                            id=str(field.get("id", "")),
                            name=str(field.get("name", field.get("id", ""))),
                            type=str(field.get("type", "text")),
                        )
                        for field in fields
                        if field.get("id")
                    ],
                    sampleRows=list(records)[:sample_limit],
                )
            )
        return TableContextResult(tables=tables)

    def _build_work_tags(self, request: EstimateWorkloadRequest) -> list[str]:
        tags = list(request.tags)
        tags.extend(request.tech_stack.primary_stack)
        tags.extend(request.tech_stack.architecture)
        if request.business_domain:
            tags.append(request.business_domain)
        for requirement in request.non_functional_requirements:
            tags.extend(list(_tokenize(requirement)))
        return sorted({item.strip().lower() for item in tags if str(item).strip()})

    def _build_stack_tags(self, request: EstimateWorkloadRequest) -> list[str]:
        tags = []
        tags.extend(request.tech_stack.primary_stack)
        tags.extend(request.tech_stack.architecture)
        tags.extend(request.tech_stack.integrations)
        return sorted({item.strip().lower() for item in tags if str(item).strip()})

    def _score_history_row(
        self,
        request: EstimateWorkloadRequest,
        row: WorkloadEstimateRun,
    ) -> float:
        prompt_tokens = _tokenize(request.prompt)
        row_tokens = _tokenize(f"{row.normalized_scope} {row.source_prompt}")
        prompt_score = len(prompt_tokens & row_tokens) / max(len(prompt_tokens | row_tokens), 1)

        requested_stack = {item.lower() for item in request.tech_stack.primary_stack}
        historical_stack = {str(item).lower() for item in (row.tech_stack_tags or [])}
        stack_score = len(requested_stack & historical_stack) / max(
            len(requested_stack | historical_stack),
            1,
        )

        request_tags = set(self._build_work_tags(request))
        row_tags = {str(item).lower() for item in (row.work_type_tags or [])}
        tag_score = len(request_tags & row_tags) / max(len(request_tags | row_tags), 1)

        domain_bonus = 0.15 if request.business_domain and request.business_domain == row.business_domain else 0.0
        feedback_bonus = 0.15 if row.actual_hours is not None or row.actual_story_points is not None else 0.0
        workspace_bonus = 0.1 if request.workspace_id and request.workspace_id == row.workspace_id else 0.0

        score = (prompt_score * 0.45) + (stack_score * 0.2) + (tag_score * 0.1) + domain_bonus + feedback_bonus + workspace_bonus
        return _clamp(score, 0.0, 1.0)

    def _serialize_history_signal(
        self,
        row: WorkloadEstimateRun,
        *,
        similarity: float,
    ) -> HistoricalEstimateSignal:
        return HistoricalEstimateSignal(
            estimateId=row.id,
            title=row.normalized_scope,
            similarity=_round_metric(similarity),
            sourceType="feedback" if row.actual_hours is not None or row.actual_story_points is not None else "estimate",
            estimatedStoryPoints=row.adjusted_story_points,
            actualStoryPoints=row.actual_story_points,
            estimatedHours=row.p50_hours,
            actualHours=row.actual_hours,
            summary=(row.structured_output or {}).get("summary", "")[:240],
            techStack=list(row.tech_stack_tags or []),
        )

    def _build_learning_summary_from_rows(
        self,
        rows: list[tuple[WorkloadEstimateRun, float]],
    ) -> HistoricalLearningSummary:
        signals = [self._serialize_history_signal(row, similarity=score) for row, score in rows]
        feedback_rows = [row for row, _ in rows if row.actual_hours is not None or row.actual_story_points is not None]

        hours_per_sp_values: list[float] = []
        bias_values: list[float] = []
        for row in feedback_rows:
            denominator = row.actual_story_points or row.adjusted_story_points
            if denominator and denominator > 0 and row.actual_hours is not None:
                hours_per_sp_values.append(float(row.actual_hours) / float(denominator))
            if row.actual_hours is not None and row.p50_hours:
                bias_values.append(float(row.actual_hours) / max(float(row.p50_hours), 0.1))

        notes = []
        if feedback_rows:
            notes.append("历史样本包含真实反馈，可用于修正 hours/story point 与估算偏差。")
        else:
            notes.append("未找到足够反馈样本，历史学习以过往估算记录为弱信号。")

        return HistoricalLearningSummary(
            sampleCount=len(rows),
            feedbackSampleCount=len(feedback_rows),
            averageHoursPerStoryPoint=_round_metric(mean(hours_per_sp_values) if hours_per_sp_values else 0.0),
            averageBias=_round_metric(mean(bias_values) if bias_values else 1.0),
            notes=notes,
            examples=signals,
        )

    async def _load_historical_learning(
        self,
        db: AsyncSession,
        *,
        request: EstimateWorkloadRequest,
        agent_context: AgentContext,
        user_id: int,
    ) -> HistoricalLearningSummary:
        if not request.historical_learning.enabled:
            return HistoricalLearningSummary(notes=["历史学习已禁用。"])

        cache_key = self._history_cache_key(
            request,
            agent_context,
            user_id=user_id,
        )
        cached = await self._get_cached_json(cache_key)
        if cached is not None:
            return HistoricalLearningSummary.model_validate(cached)

        threshold = _now_utc() - timedelta(days=request.historical_learning.lookback_days)
        query = select(WorkloadEstimateRun).where(
            WorkloadEstimateRun.created_at >= threshold,
            WorkloadEstimateRun.created_by == str(user_id),
            WorkloadEstimateRun.status.in_(
                ["estimated", "applied", "feedback_applied"]
            ),
        )
        if request.workspace_id:
            query = query.where(
                WorkloadEstimateRun.workspace_id == request.workspace_id
            )
        if request.project_id:
            query = query.where(
                WorkloadEstimateRun.project_id == request.project_id
            )
        if request.historical_learning.require_feedback:
            query = query.where(
                or_(
                    WorkloadEstimateRun.actual_hours.is_not(None),
                    WorkloadEstimateRun.actual_story_points.is_not(None),
                )
            )
        result = await db.execute(query.order_by(WorkloadEstimateRun.created_at.desc()).limit(80))

        scored_rows: list[tuple[WorkloadEstimateRun, float]] = []
        for row in result.scalars().all():
            score = self._score_history_row(request, row)
            if score >= 0.08:
                scored_rows.append((row, score))

        scored_rows.sort(key=lambda item: item[1], reverse=True)
        selected = scored_rows[: request.historical_learning.limit]
        summary = self._build_learning_summary_from_rows(selected)
        await self._set_cached_json(
            cache_key,
            summary.model_dump(mode="json", by_alias=True),
            ttl_seconds=300,
        )
        return summary

    def _build_system_prompt(
        self,
        request: EstimateWorkloadRequest,
        agent_context: AgentContext,
        history: HistoricalLearningSummary,
    ) -> str:
        return (
            "你是 QTable 的 estimate_workload Agent，负责对复杂项目进行可审计、可学习、可复用的工作量评估。\n\n"
            "核心要求：\n"
            "1. 必须输出 Story Point、P50/P90、风险系数、团队能力修正、技术栈影响、历史学习信号、输出可信度。\n"
            "2. 优先估算真实交付工作量，而不是理论最小实现成本。\n"
            "3. 风险系数越高，P90 应明显高于 P50。\n"
            "4. teamCapabilityFactor > 1 表示团队能力不足导致工期上浮；< 1 表示团队能力较强可降低交付成本。\n"
            "5. techStackFactor > 1 表示技术栈或集成复杂度带来额外成本；< 1 表示已有成熟资产可复用。\n"
            "6. historicalAdjustmentFactor 必须显式体现历史样本学习结果，若样本不足则保持接近 1.0 并说明原因。\n"
            "7. confidence.score 取值 0~1，必须与上下文完备度、历史样本质量、需求清晰度一致。\n"
            "8. breakdown 至少覆盖 3 个可交付工作块；每个工作块都要给出 SP 与 P50/P90。\n"
            "9. 不得编造数据库中不存在的历史事实；若信息不足，写入 assumptions 或 warnings。\n"
            "10. 输出必须严格符合 Pydantic schema，不要输出 markdown。\n\n"
            "建议估算公式：\n"
            "adjustedStoryPoints = baseStoryPoints * riskCoefficient * techStackFactor * historicalAdjustmentFactor * teamCapabilityFactor\n"
            "p50Hours 可参考 adjustedStoryPoints 与历史 hours/story point；p90Hours 需反映风险尾部。\n\n"
            "当前上下文：\n"
            f"- scopeKey: {agent_context.scope_key}\n"
            f"- workspace: {agent_context.workspace.name or agent_context.workspace.id or 'unknown'}\n"
            f"- projectId: {request.project_id or agent_context.project.id or 'unknown'}\n"
            f"- taskId: {request.task_id or agent_context.task.id or 'unknown'}\n"
            f"- qualityBar: {request.quality_bar}\n"
            f"- businessDomain: {request.business_domain or 'unknown'}\n"
            f"- historySamples: {history.sample_count}\n"
            f"- feedbackSamples: {history.feedback_sample_count}\n"
            f"- contextSummary: {agent_context.summary.text}\n"
        )

    def _build_user_prompt(
        self,
        request: EstimateWorkloadRequest,
        history: HistoricalLearningSummary,
        agent_context: AgentContext,
    ) -> str:
        history_text = "\n".join(
            [
                f"- {item.title} | similarity={item.similarity} | estSP={item.estimated_story_points} | actualHours={item.actual_hours}"
                for item in history.examples[:6]
            ]
        ) or "- 无足够历史样本"
        highlights = "\n".join(f"- {item}" for item in agent_context.summary.highlights[:10]) or "- 无"
        return (
            f"用户目标：{request.prompt}\n\n"
            "团队画像：\n"
            f"- teamSize: {request.team_profile.team_size}\n"
            f"- avgSeniority: {request.team_profile.avg_seniority}\n"
            f"- domainFamiliarity: {request.team_profile.domain_familiarity}\n"
            f"- stackFamiliarity: {request.team_profile.stack_familiarity}\n"
            f"- deliveryMaturity: {request.team_profile.delivery_maturity}\n"
            f"- parallelStreams: {request.team_profile.parallel_streams}\n\n"
            "技术栈画像：\n"
            f"- primaryStack: {request.tech_stack.primary_stack}\n"
            f"- architecture: {request.tech_stack.architecture}\n"
            f"- integrations: {request.tech_stack.integrations}\n"
            f"- unknowns: {request.tech_stack.unknowns}\n"
            f"- legacySurface: {request.tech_stack.legacy_surface}\n\n"
            f"非功能要求：{request.non_functional_requirements}\n"
            f"参考链接：{request.reference_urls}\n"
            f"业务标签：{request.tags}\n\n"
            "上下文亮点：\n"
            f"{highlights}\n\n"
            "历史学习摘要：\n"
            f"- averageHoursPerStoryPoint: {history.average_hours_per_story_point}\n"
            f"- averageBias: {history.average_bias}\n"
            f"{history_text}\n\n"
            "请给出完整估算结果，并确保 breakdown、topRisks、confidence、historicalLearning、formula、nextActions 都有内容。"
        )

    def _normalize_breakdown(self, items: list[WorkloadBreakdownItem]) -> list[WorkloadBreakdownItem]:
        normalized: list[WorkloadBreakdownItem] = []
        for item in items[:8]:
            p50 = max(item.p50_hours, 1.0)
            p90 = max(item.p90_hours, p50)
            normalized.append(
                WorkloadBreakdownItem(
                    name=item.name.strip(),
                    description=item.description.strip(),
                    storyPoints=max(item.story_points, 0.5),
                    p50Hours=_round_metric(p50),
                    p90Hours=_round_metric(p90),
                    riskCoefficient=_round_metric(_clamp(item.risk_coefficient, 0.5, 3.0)),
                    assumptions=[text.strip() for text in item.assumptions if str(text).strip()][:6],
                )
            )
        return normalized

    def _normalize_result(
        self,
        raw: EstimateWorkloadResult,
        history: HistoricalLearningSummary,
        agent_context: AgentContext,
    ) -> EstimateWorkloadResult:
        breakdown = self._normalize_breakdown(raw.breakdown)
        base_sp = max(raw.base_story_points, sum(item.story_points for item in breakdown) or 1.0)
        risk = _round_metric(_clamp(raw.risk_coefficient, 0.5, 3.0))
        team = _round_metric(_clamp(raw.team_capability_factor, 0.5, 2.0))
        stack = _round_metric(_clamp(raw.tech_stack_factor, 0.5, 2.0))
        hist = _round_metric(_clamp(raw.historical_adjustment_factor, 0.5, 2.0))
        adjusted_sp = max(raw.adjusted_story_points, base_sp * risk * team * stack * hist)
        p50 = max(raw.p50_hours, sum(item.p50_hours for item in breakdown) or adjusted_sp * 5)
        p90 = max(raw.p90_hours, sum(item.p90_hours for item in breakdown) or p50 * max(1.2, risk))
        confidence_score = _round_metric(_clamp(raw.confidence.score, 0.0, 1.0))

        if confidence_score >= 0.75:
            confidence_level = "high"
        elif confidence_score >= 0.45:
            confidence_level = "medium"
        else:
            confidence_level = "low"

        return EstimateWorkloadResult(
            summary=raw.summary.strip(),
            scope=raw.scope.strip(),
            baseStoryPoints=_round_metric(base_sp),
            adjustedStoryPoints=_round_metric(adjusted_sp),
            p50Hours=_round_metric(p50),
            p90Hours=_round_metric(max(p90, p50)),
            riskCoefficient=risk,
            teamCapabilityFactor=team,
            techStackFactor=stack,
            historicalAdjustmentFactor=hist,
            confidence=EstimateConfidence(
                score=confidence_score,
                level=confidence_level,
                rationale=raw.confidence.rationale.strip() or "基于上下文、历史样本与需求清晰度综合判断。",
            ),
            contextInsight=ContextInsight(
                summary=raw.context_insight.summary.strip() or agent_context.summary.text[:300],
                signals=[item.strip() for item in raw.context_insight.signals if str(item).strip()][:8]
                or list(agent_context.summary.highlights[:5]),
            ),
            historicalLearning=HistoricalLearningSummary(
                sampleCount=max(raw.historical_learning.sample_count, history.sample_count),
                feedbackSampleCount=max(
                    raw.historical_learning.feedback_sample_count,
                    history.feedback_sample_count,
                ),
                averageHoursPerStoryPoint=(
                    raw.historical_learning.average_hours_per_story_point
                    or history.average_hours_per_story_point
                ),
                averageBias=raw.historical_learning.average_bias or history.average_bias,
                notes=(
                    [item.strip() for item in raw.historical_learning.notes if str(item).strip()][:6]
                    or history.notes
                ),
                examples=raw.historical_learning.examples[:6] or history.examples[:6],
            ),
            breakdown=breakdown,
            topRisks=raw.top_risks[:6] or [
                WorkloadRisk(
                    title="需求边界待确认",
                    level="medium",
                    impact="可能导致工作量重复评估或返工",
                    mitigation="补充范围定义与验收标准",
                )
            ],
            assumptions=[item.strip() for item in raw.assumptions if str(item).strip()][:10],
            warnings=[item.strip() for item in raw.warnings if str(item).strip()][:10],
            formula=raw.formula.strip()
            or "adjustedStoryPoints = baseStoryPoints * riskCoefficient * techStackFactor * historicalAdjustmentFactor * teamCapabilityFactor",
            nextActions=[item.strip() for item in raw.next_actions if str(item).strip()][:8]
            or ["将估算结果转化为里程碑与任务拆解，并在执行后回填实际工时。"],
        )

    async def _persist_result(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: EstimateWorkloadRequest,
        trace_id: str,
        provider: str,
        model: str,
        agent_context: AgentContext,
        history: HistoricalLearningSummary,
        result: EstimateWorkloadResult,
    ) -> str:
        estimate_id = str(uuid.uuid4())
        row = WorkloadEstimateRun(
            id=estimate_id,
            workspace_id=request.workspace_id,
            project_id=request.project_id,
            task_id=request.task_id,
            team_id=request.team_id,
            source_prompt=request.prompt,
            normalized_scope=result.scope[:255],
            business_domain=request.business_domain,
            quality_bar=request.quality_bar,
            tech_stack_tags=self._build_stack_tags(request),
            work_type_tags=self._build_work_tags(request),
            request_payload=request.model_dump(mode="json", by_alias=True),
            context_summary=agent_context.summary.model_dump(mode="json", by_alias=True),
            historical_summary=history.model_dump(mode="json", by_alias=True),
            structured_output=result.model_dump(mode="json", by_alias=True),
            confidence_score=result.confidence.score,
            base_story_points=result.base_story_points,
            adjusted_story_points=result.adjusted_story_points,
            p50_hours=result.p50_hours,
            p90_hours=result.p90_hours,
            risk_coefficient=result.risk_coefficient,
            team_capability_factor=result.team_capability_factor,
            tech_stack_factor=result.tech_stack_factor,
            historical_adjustment_factor=result.historical_adjustment_factor,
            status="estimated",
            provider=provider,
            model=model,
            trace_id=trace_id,
            created_by=str(user_id),
        )
        db.add(row)
        await db.commit()
        await self.invalidate_history_cache(request.workspace_id)
        return estimate_id

    def _build_agent(
        self,
        *,
        provider_model,
        request: EstimateWorkloadRequest,
        agent_context: AgentContext,
        history: HistoricalLearningSummary,
    ) -> Agent[EstimateWorkloadDeps, EstimateWorkloadResult]:
        agent = Agent(
            model=provider_model,
            deps_type=EstimateWorkloadDeps,
            output_type=EstimateWorkloadResult,
            system_prompt=self._build_system_prompt(request, agent_context, history),
            model_settings=OpenAIChatModelSettings(temperature=0.15),
            retries={
                "output": request.retry.max_output_retries,
                "tools": request.retry.max_tool_retries,
            },
        )

        @agent.instructions
        def runtime_instructions(_ctx: RunContext[EstimateWorkloadDeps]) -> str:
            return (
                "补充要求：\n"
                "- 估算必须体现复杂度、实施成本、验证成本、联调成本、风险缓冲。\n"
                "- 若历史样本不足，不要强行拉高或拉低 historicalAdjustmentFactor。\n"
                "- Story Point 与 Hours 必须相互一致，避免 breakdown 总和与总估算严重偏离。\n"
            )

        @agent.output_validator
        def validate_output(
            _ctx: RunContext[EstimateWorkloadDeps],
            output: EstimateWorkloadResult,
        ) -> EstimateWorkloadResult:
            if len(output.breakdown) < 2:
                raise ModelRetry("breakdown 至少需要 2 个工作块")
            if output.p90_hours < output.p50_hours:
                raise ModelRetry("p90Hours 必须大于等于 p50Hours")
            if not output.top_risks:
                raise ModelRetry("topRisks 不能为空")
            if not output.next_actions:
                raise ModelRetry("nextActions 不能为空")
            return output

        @agent.tool(
            name="load_table_context",
            retries=request.retry.max_tool_retries,
            include_return_schema=True,
        )
        async def load_table_context(
            ctx: RunContext[EstimateWorkloadDeps],
            payload: TableContextArgs,
        ) -> TableContextResult:
            started = time.perf_counter()
            result = await self._load_table_context(
                ctx.deps.db,
                table_ids=payload.table_ids or ctx.deps.request.table_ids,
                sample_limit=payload.sample_limit,
                user_id=ctx.deps.user_id,
            )
            ctx.deps.tool_log.append(
                {
                    "tool": "load_table_context",
                    "latencyMs": round((time.perf_counter() - started) * 1000, 3),
                    "arguments": payload.model_dump(mode="json", by_alias=True),
                }
            )
            return result

        @agent.tool(
            name="load_historical_estimates",
            retries=request.retry.max_tool_retries,
            include_return_schema=True,
        )
        async def load_historical_estimates(
            ctx: RunContext[EstimateWorkloadDeps],
            payload: HistoricalEstimateArgs,
        ) -> HistoricalEstimateResult:
            started = time.perf_counter()
            result = HistoricalEstimateResult.model_validate(
                ctx.deps.history.model_dump(mode="json", by_alias=True)
            )
            result.examples = result.examples[: payload.limit]
            ctx.deps.tool_log.append(
                {
                    "tool": "load_historical_estimates",
                    "latencyMs": round((time.perf_counter() - started) * 1000, 3),
                    "arguments": payload.model_dump(mode="json", by_alias=True),
                }
            )
            return result

        return agent

    async def run(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: EstimateWorkloadRequest,
    ) -> EstimateWorkloadResponse:
        provider_model, provider_name, model_name = await self._load_provider_model(
            db,
            user_id=user_id,
            model_override=request.model,
        )

        # Fallback: 仅 deepseek-reasoner (R1) 不支持工具调用，
        # V4 模型由 deepseek_helper HTTP Transport 层处理兼容性，不需要 fallback
        if not tool_router_service.model_supports_tool_calling(model_name):
            fallback_model = settings.DEEPSEEK_MODEL or "deepseek-chat"
            logger.warning(
                "estimate_workload: model %s does not support tool calling, falling back to %s",
                model_name,
                fallback_model,
            )
            model_name = fallback_model
            provider_model, provider_name, _ = await self._load_provider_model(
                db,
                user_id=user_id,
                model_override=fallback_model,
            )
        trace_id = str(uuid.uuid4())
        agent_context = await self._build_agent_context(
            db,
            user_id=user_id,
            request=request,
            trace_id=trace_id,
        )
        history = await self._load_historical_learning(
            db,
            request=request,
            agent_context=agent_context,
            user_id=user_id,
        )
        last_error: Exception | None = None

        for attempt in range(1, request.retry.max_attempts + 1):
            deps = EstimateWorkloadDeps(
                db=db,
                user_id=user_id,
                request=request,
                agent_context=agent_context,
                history=history,
            )
            try:
                agent = self._build_agent(
                    provider_model=provider_model,
                    request=request,
                    agent_context=agent_context,
                    history=history,
                )
                result = await agent.run(
                    self._build_user_prompt(request, history, agent_context),
                    deps=deps,
                    retries={"output": request.retry.max_output_retries},
                )
                normalized = self._normalize_result(result.output, history, agent_context)
                persisted = bool(request.persist_result and not request.dry_run)
                estimate_id = None
                if persisted:
                    estimate_id = await self._persist_result(
                        db,
                        user_id=user_id,
                        request=request,
                        trace_id=trace_id,
                        provider=provider_name,
                        model=model_name,
                        agent_context=agent_context,
                        history=history,
                        result=normalized,
                    )
                return EstimateWorkloadResponse(
                    traceId=trace_id,
                    provider=provider_name,
                    model=model_name,
                    attempts=attempt,
                    persisted=persisted,
                    estimateId=estimate_id,
                    result=normalized,
                )
            except Exception as exc:
                last_error = exc
                exc_msg = str(exc)

                # Check if the error is due to the model not supporting tool calling
                if tool_router_service.is_tool_calling_unsupported_error(exc_msg):
                    logger.error(
                        "estimate_workload model_unsupported trace_id=%s model=%s error=%s",
                        trace_id,
                        model_name,
                        exc_msg,
                    )
                    return EstimateWorkloadResponse(
                        traceId=trace_id,
                        provider=provider_name,
                        model=model_name,
                        attempts=attempt,
                        persisted=False,
                        estimateId=None,
                        result=None,
                        error={
                            "code": "MODEL_TOOL_CALLING_UNSUPPORTED",
                            "message": f"当前模型 {model_name} 不支持工具调用，请切换到支持 Function Calling 的模型，例如 deepseek-chat。",
                        },
                    )

                logger.warning(
                    "estimate_workload attempt failed trace_id=%s attempt=%s error=%s",
                    trace_id,
                    attempt,
                    exc_msg,
                )
                await db.rollback()
                if attempt < request.retry.max_attempts and request.retry.backoff_ms:
                    await asyncio.sleep(request.retry.backoff_ms / 1000)

        failed = EstimateWorkloadResult(
            summary="工作量评估失败",
            scope=request.prompt,
            baseStoryPoints=3.0,
            adjustedStoryPoints=3.0,
            p50Hours=16.0,
            p90Hours=24.0,
            riskCoefficient=1.0,
            teamCapabilityFactor=1.0,
            techStackFactor=1.0,
            historicalAdjustmentFactor=1.0,
            confidence=EstimateConfidence(
                score=0.0,
                level="low",
                rationale="模型多次重试后仍未输出有效估算。",
            ),
            contextInsight=ContextInsight(
                summary=agent_context.summary.text[:300],
                signals=list(agent_context.summary.highlights[:5]),
            ),
            historicalLearning=history,
            breakdown=[
                WorkloadBreakdownItem(
                    name="重新澄清范围",
                    description="当前估算失败，需要缩小范围并重新确认目标。",
                    storyPoints=3.0,
                    p50Hours=16.0,
                    p90Hours=24.0,
                    riskCoefficient=1.0,
                    assumptions=["需补充更明确的业务边界和验收标准。"],
                )
            ],
            topRisks=[
                WorkloadRisk(
                    title="需求信息不足",
                    level="high",
                    impact="无法形成稳定估算",
                    mitigation="补充项目范围、接口依赖与质量要求后重试",
                )
            ],
            assumptions=[],
            warnings=[str(last_error) or "unknown error"],
            formula="adjustedStoryPoints = baseStoryPoints * riskCoefficient * techStackFactor * historicalAdjustmentFactor * teamCapabilityFactor",
            nextActions=["检查模型配置、补充上下文，或将任务拆分后重新评估。"],
        )
        return EstimateWorkloadResponse(
            traceId=trace_id,
            provider=provider_name,
            model=model_name,
            attempts=request.retry.max_attempts,
            persisted=False,
            estimateId=None,
            result=failed,
            error={
                "code": "ESTIMATE_WORKLOAD_FAILED",
                "message": str(last_error) or "Estimate workload failed",
            },
        )

    async def submit_feedback(
        self,
        db: AsyncSession,
        *,
        estimate_id: str,
        user_id: int,
        feedback: EstimateFeedbackRequest,
    ) -> WorkloadEstimateRun | None:
        result = await db.execute(
            select(WorkloadEstimateRun)
            .where(
                WorkloadEstimateRun.id == estimate_id,
                WorkloadEstimateRun.created_by == str(user_id),
            )
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return None
        row.actual_story_points = feedback.actual_story_points
        row.actual_hours = feedback.actual_hours
        row.outcome_status = feedback.outcome_status
        row.accuracy_rating = feedback.accuracy_rating
        row.feedback_notes = feedback.notes
        row.feedback_at = _now_utc()
        row.status = "feedback_applied"
        await db.commit()
        await self.invalidate_history_cache(row.workspace_id)
        return row


estimate_workload_service = EstimateWorkloadService()
