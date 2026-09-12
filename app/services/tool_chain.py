from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import redis.asyncio as redis
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModelSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.tool_chain import ToolChainEventLog, ToolChainRun, ToolChainStepRun
from app.schemas.tool_chain import (
    ToolChainContextState,
    ToolChainEvent,
    ToolChainPlan,
    ToolChainRequest,
    ToolChainRollbackPolicy,
    ToolChainRunResponse,
    ToolChainStepDefinition,
    ToolChainStepState,
)
from app.services.ai_tool_router import tool_router_service
from app.services.skill_registry import skill_registry_service
from app.services.smart_table_store import delete_record, delete_record_file
from app.skills.contracts import SkillContext
from app.skills.runtime import SkillExecutionContext, SkillRuntime
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

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ToolChainRuntimeService:
    RUN_CACHE_PREFIX = "tool-chain-run"

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

    def _normalize_context(self, request: ToolChainRequest) -> ToolChainContextState:
        return ToolChainContextState.model_validate(
            {
                **request.context.model_dump(mode="json", by_alias=True),
                "sessionId": request.session_id or request.context.session_id,
                "workspaceId": request.workspace_id or request.context.workspace_id,
                "conversationId": request.conversation_id or request.context.conversation_id,
                "projectId": request.project_id or request.context.project_id,
                "tableIds": request.table_ids or request.context.table_ids,
                "viewId": request.view_id or request.context.view_id,
                "taskId": request.task_id or request.context.task_id,
                "teamId": request.team_id or request.context.team_id,
                "organizationId": request.organization_id or request.context.organization_id,
                "workflowId": request.workflow_id or request.context.workflow_id,
                "agentId": request.agent_id or request.context.agent_id,
            }
        )

    def _build_execution_context(
        self,
        *,
        user_id: int,
        request: ToolChainRequest,
        context: ToolChainContextState,
        trace_id: str,
        db: AsyncSession,
    ) -> SkillExecutionContext:
        return SkillExecutionContext(
            context=SkillContext(
                user_id=user_id,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                conversation_id=context.conversation_id,
                project_id=context.project_id,
                table_ids=context.table_ids,
                view_id=context.view_id,
                task_id=context.task_id,
                team_id=context.team_id,
                organization_id=context.organization_id,
                workflow_id=context.workflow_id,
                agent_id=context.agent_id,
                trace_id=trace_id,
                origin="workflow",
                locale=request.locale,
                timezone=request.timezone,
                dry_run=request.dry_run,
                confirmed=request.confirmed,
            ),
            db=db,
        )

    def _available_skill_lines(self, manifests: list[Any]) -> str:
        lines: list[str] = []
        for manifest in manifests:
            meta = manifest.metadata
            lines.append(
                f"- {meta.name}: {meta.description} | sideEffect={meta.side_effect.value} | "
                f"confirm={meta.confirmation_required}"
            )
        return "\n".join(lines)

    def _build_recommended_chain(self, skill_names: set[str], request: ToolChainRequest) -> list[str]:
        recommended: list[str] = []
        if "qtable.task.split" in skill_names:
            recommended.append("qtable.task.split")
        if "qtable.project.estimate_workload" in skill_names:
            recommended.append("qtable.project.estimate_workload")
        if "qtable.gantt.generate" in skill_names:
            recommended.append("qtable.gantt.generate")
        if "qtable.task.records.create" in skill_names and request.table_ids:
            recommended.append("qtable.task.records.create")
        return recommended

    def _fallback_plan(self, request: ToolChainRequest, manifests: list[Any]) -> ToolChainPlan:
        skill_names = {manifest.metadata.name for manifest in manifests}
        steps: list[ToolChainStepDefinition] = []
        if "qtable.task.split" in skill_names:
            steps.append(
                ToolChainStepDefinition(
                    stepId="split_task",
                    skillName="qtable.task.split",
                    description="拆分需求，生成任务树和依赖。",
                    arguments={
                        "prompt": "{{context.request.message}}",
                        "autoCreateRecords": False,
                        "dryRun": "{{context.request.dryRun}}",
                        "tableIds": "{{context.request.tableIds}}",
                    },
                    saveResultAs="taskSplit",
                )
            )
        if "qtable.project.estimate_workload" in skill_names:
            steps.append(
                ToolChainStepDefinition(
                    stepId="estimate_workload",
                    skillName="qtable.project.estimate_workload",
                    description="结合目标和拆解结果估算工作量。",
                    dependsOn=["split_task"] if steps else [],
                    arguments={
                        "prompt": "{{context.request.message}}",
                        "qualityBar": "enterprise",
                        "dryRun": "{{context.request.dryRun}}",
                        "tags": ["tool-chain", "enterprise"],
                    },
                    saveResultAs="workloadEstimate",
                )
            )
        if "qtable.gantt.generate" in skill_names and any(step.step_id == "split_task" for step in steps):
            steps.append(
                ToolChainStepDefinition(
                    stepId="generate_gantt",
                    skillName="qtable.gantt.generate",
                    description="从任务树生成 Gantt 数据。",
                    dependsOn=["split_task"],
                    arguments={
                        "taskPlan": "{{steps.split_task.output.result}}",
                        "startDate": "{{context.variables.projectStartDate}}",
                    },
                    saveResultAs="ganttData",
                )
            )
        if "qtable.task.records.create" in skill_names and request.table_ids:
            # Add schema describe step first to understand table structure before writing
            if "qtable.schema.describe" in skill_names:
                steps.append(
                    ToolChainStepDefinition(
                        stepId="describe_target_table",
                        skillName="qtable.schema.describe",
                        description="获取目标数据表结构，确保后续写入的任务字段与表格式匹配。",
                        arguments={
                            "tableId": request.table_ids[0],
                        },
                        saveResultAs="targetTableSchema",
                    )
                )

        if "qtable.task.records.create" in skill_names and request.table_ids and any(
            step.step_id == "split_task" for step in steps
        ):
            depends_on = ["split_task"]
            if any(step.step_id == "generate_gantt" for step in steps):
                depends_on.append("generate_gantt")
            if any(step.step_id == "describe_target_table" for step in steps):
                depends_on.append("describe_target_table")
            steps.append(
                ToolChainStepDefinition(
                    stepId="create_task_records",
                    skillName="qtable.task.records.create",
                    description="将任务树落库到目标任务表。",
                    dependsOn=depends_on,
                    arguments={
                        "tableId": request.table_ids[0],
                        "taskPlan": "{{steps.split_task.output.result}}",
                        "ganttData": "{{steps.generate_gantt.output}}",
                        "dryRun": "{{context.request.dryRun}}",
                    },
                    saveResultAs="createdTaskRecords",
                )
            )
        summary = " -> ".join(step.skill_name for step in steps) or "no-op"
        return ToolChainPlan(
            goal=request.message,
            summary=f"Fallback chain: {summary}",
            reasoning=[
                "优先复用已注册 Skill 构造最短闭环。",
                "拆解与估算优先，若存在目标表则补充落库。",
            ],
            steps=steps,
            finalOutputPath="{{memory}}",
            plannerMetadata={"source": "fallback"},
            langGraphSpec=self._build_langgraph_spec(steps),
        )

    async def _plan_chain(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: ToolChainRequest,
        manifests: list[Any],
    ) -> tuple[ToolChainPlan, str | None, str | None]:
        if request.plan is not None:
            provided = request.plan.model_copy(
                update={"langgraph_spec": self._build_langgraph_spec(request.plan.steps)}
            )
            return provided, None, request.model

        fallback = self._fallback_plan(request, manifests)
        if not request.auto_plan:
            return fallback, None, request.model

        try:
            ai_config = await tool_router_service.load_user_ai_config(db, user_id)
            model, provider = await tool_router_service.build_provider_model(
                ai_config,
                model_override=request.model,
            )
        except Exception as exc:
            logger.warning("tool_chain planner uses fallback because model loading failed: %s", exc)
            return fallback, None, request.model

        skill_names = {manifest.metadata.name for manifest in manifests}
        recommended = self._build_recommended_chain(skill_names, request)
        system_prompt = (
            "你是 QTable 的 Multi Tool Chain Planner。\n"
            "你的职责是把用户目标规划为企业级可执行 Tool Chain。\n"
            "必须输出结构化 ToolChainPlan，且只允许使用可用 Skill 列表里的 skillName。\n"
            "要求：\n"
            "1. 优先构造最少但足够的链路。\n"
            "2. 每个 stepId 必须唯一、稳定、可读。\n"
            "3. 可使用占位符 `{{context.request.xxx}}`、`{{context.variables.xxx}}`、"
            "`{{steps.step_id.output.xxx}}`、`{{memory.xxx}}` 做上下文传递。\n"
            "4. 有依赖时必须放在 dependsOn。\n"
            "5. 写操作尽量放在链路后段，并考虑 rollback。\n"
            "6. `langGraphSpec` 需给出 nodes/edges，便于后续兼容 LangGraph。\n"
            "7. 如果目标是规划/拆解类问题，优先考虑 `qtable.task.split` 和 "
            "`qtable.project.estimate_workload`。\n"
            "8. 如果存在任务表并且需要闭环，可考虑 `qtable.task.records.create` "
            "和 `qtable.gantt.generate`。\n"
            "9. 【重要】在执行 `qtable.task.records.create` 写入任务记录前，必须先调用 "
            "`qtable.schema.describe` 获取目标表的完整 Schema，确保写入的字段值（如 STATUS、OWNER）"
            "与表的字段类型和枚举值匹配。`qtable.schema.describe` 必须作为 `create_task_records` "
            "的直接依赖。\n"
        )
        user_prompt = (
            f"用户目标:\n{request.message}\n\n"
            f"工作区上下文:\n"
            f"- workspaceId: {request.workspace_id or request.context.workspace_id}\n"
            f"- tableIds: {request.table_ids or request.context.table_ids}\n"
            f"- projectId: {request.project_id or request.context.project_id}\n"
            f"- taskId: {request.task_id or request.context.task_id}\n"
            f"- locale: {request.locale}\n"
            f"- timezone: {request.timezone}\n\n"
            f"推荐链路参考: {recommended or ['按实际需要规划']}\n\n"
            f"当前可用 Skills:\n{self._available_skill_lines(manifests)}\n\n"
            "请输出一个能闭环执行的 ToolChainPlan。"
        )
        try:
            agent = Agent(
                model=model,
                output_type=ToolChainPlan,
                system_prompt=system_prompt,
                model_settings=OpenAIChatModelSettings(temperature=0.1),
            )
            result = await agent.run(user_prompt)
            plan = result.output.model_copy(
                update={
                    "langgraph_spec": self._build_langgraph_spec(result.output.steps),
                    "planner_metadata": {
                        **result.output.planner_metadata,
                        "source": "ai_planner",
                        "recommendedChain": recommended,
                    },
                }
            )
            if not plan.steps:
                return fallback, provider.provider, provider.model
            return plan, provider.provider, provider.model
        except Exception as exc:
            logger.warning("tool_chain planner fallback triggered: %s", exc)
            return fallback, provider.provider, provider.model

    def _build_langgraph_spec(self, steps: list[ToolChainStepDefinition]) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "id": step.step_id,
                    "type": "skill",
                    "skillName": step.skill_name,
                }
                for step in steps
            ],
            "edges": [
                {"from": dep, "to": step.step_id}
                for step in steps
                for dep in step.depends_on
            ],
        }

    def _build_step_states(self, plan: ToolChainPlan) -> dict[str, ToolChainStepState]:
        states: dict[str, ToolChainStepState] = {}
        for step in plan.steps:
            states[step.step_id] = ToolChainStepState(
                stepId=step.step_id,
                skillName=step.skill_name,
                description=step.description,
                dependsOn=list(step.depends_on),
                state="pending",
                attemptCount=0,
                inputPayload={},
                rollback=step.rollback.model_dump(mode="json", by_alias=True),
            )
        return states

    async def _create_run_row(
        self,
        *,
        db: AsyncSession,
        run_id: str,
        trace_id: str,
        user_id: int,
        request: ToolChainRequest,
        context: ToolChainContextState,
        status: str,
        plan: ToolChainPlan | None,
        provider: str | None,
        model: str | None,
    ) -> ToolChainRun:
        row = ToolChainRun(
            id=run_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            conversation_id=context.conversation_id,
            project_id=context.project_id,
            view_id=context.view_id,
            task_id=context.task_id,
            team_id=context.team_id,
            organization_id=context.organization_id,
            workflow_id=context.workflow_id,
            agent_id=context.agent_id,
            source_message=request.message,
            status=status,
            confirmed=request.confirmed,
            dry_run=request.dry_run,
            provider=provider,
            model=model,
            trace_id=trace_id,
            request_payload=request.model_dump(mode="json", by_alias=True),
            plan_json=plan.model_dump(mode="json", by_alias=True) if plan else None,
            context_json=context.model_dump(mode="json", by_alias=True),
            memory_json=context.memory,
            result_json={},
            summary="",
            created_by=str(user_id),
            started_at=_now_utc(),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    async def _sync_step_rows(
        self,
        *,
        db: AsyncSession,
        run_id: str,
        plan: ToolChainPlan,
        step_states: dict[str, ToolChainStepState],
    ) -> dict[str, ToolChainStepRun]:
        rows: dict[str, ToolChainStepRun] = {}
        for order_index, step in enumerate(plan.steps):
            state = step_states[step.step_id]
            row = ToolChainStepRun(
                id=str(uuid.uuid4()),
                run_id=run_id,
                step_id=step.step_id,
                skill_name=step.skill_name,
                description=step.description,
                order_index=order_index,
                depends_on=list(step.depends_on),
                status=state.state,
                attempt_count=state.attempt_count,
                input_payload=state.input_payload,
                output_payload=state.output_payload,
                retry_policy=step.retry.model_dump(mode="json", by_alias=True),
                rollback_payload=step.rollback.model_dump(mode="json", by_alias=True),
                context_snapshot={},
            )
            db.add(row)
            rows[step.step_id] = row
        await db.commit()
        return rows

    async def _cache_run_snapshot(self, response: ToolChainRunResponse) -> None:
        client = await self._get_redis()
        if client is None:
            return
        key = f"{self.RUN_CACHE_PREFIX}:{response.run_id}"
        try:
            await client.setex(
                key,
                3600,
                json.dumps(response.model_dump(mode="json", by_alias=True), ensure_ascii=False, default=str),
            )
        except Exception:
            return

    async def _emit_event(
        self,
        *,
        db: AsyncSession,
        run_row: ToolChainRun,
        step_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        response_builder,
        event_sink=None,
    ) -> None:
        event = ToolChainEvent(
            type=event_type,
            runId=run_row.id,
            traceId=run_row.trace_id,
            stepId=step_id,
            data=payload,
        )
        db.add(
            ToolChainEventLog(
                id=str(uuid.uuid4()),
                run_id=run_row.id,
                trace_id=run_row.trace_id,
                step_id=step_id,
                event_type=event_type,
                payload=payload,
            )
        )
        await db.commit()
        if event_sink is not None:
            await event_sink(event)
        await self._cache_run_snapshot(response_builder())

    def _path_get(self, source: Any, path: str) -> Any:
        current = source
        for part in path.split("."):
            if part == "":
                continue
            if isinstance(current, list):
                current = current[int(part)]
            elif isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
            if current is None:
                return None
        return current

    def _resolve_value(self, value: Any, source: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._resolve_value(item, source) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(item, source) for item in value]
        if not isinstance(value, str):
            return value

        matches = list(PLACEHOLDER_RE.finditer(value))
        if not matches:
            return value
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return self._path_get(source, matches[0].group(1))

        rendered = value
        for match in matches:
            replacement = self._path_get(source, match.group(1))
            if isinstance(replacement, (dict, list)):
                replacement = json.dumps(replacement, ensure_ascii=False)
            rendered = rendered.replace(match.group(0), "" if replacement is None else str(replacement))
        return rendered

    def _build_resolution_source(
        self,
        *,
        request: ToolChainRequest,
        context: ToolChainContextState,
        memory: dict[str, Any],
        steps_output: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "context": {
                **context.model_dump(mode="json", by_alias=True),
                "request": request.model_dump(mode="json", by_alias=True),
            },
            "memory": memory,
            "steps": steps_output,
        }

    def _collect_final_result(
        self,
        *,
        request: ToolChainRequest,
        plan: ToolChainPlan,
        memory: dict[str, Any],
        steps_output: dict[str, Any],
    ) -> dict[str, Any]:
        source = self._build_resolution_source(
            request=request,
            context=self._normalize_context(request),
            memory=memory,
            steps_output=steps_output,
        )
        if plan.final_output_path:
            resolved = self._resolve_value(plan.final_output_path, source)
            return resolved if isinstance(resolved, dict) else {"value": resolved}
        if steps_output:
            last_step_id = plan.steps[-1].step_id
            return steps_output.get(last_step_id, {})
        return {}

    async def _delete_created_records(
        self,
        *,
        db: AsyncSession,
        table_id: str,
        record_ids: list[str],
    ) -> dict[str, Any]:
        deleted: list[str] = []
        backend = (settings.DATA_BACKEND or "").lower()
        for record_id in record_ids:
            try:
                if backend in {"db", "database", "postgres", "sqlite"}:
                    if await delete_record(db, table_id, record_id):
                        deleted.append(record_id)
                else:
                    if await delete_record_file(table_id, record_id):
                        deleted.append(record_id)
            except Exception:
                continue
        return {"tableId": table_id, "deletedRecordIds": deleted}

    async def _rollback_step(
        self,
        *,
        db: AsyncSession,
        request: ToolChainRequest,
        execution_context: SkillExecutionContext,
        runtime: SkillRuntime,
        tool_executor: ToolExecutor,
        adapter_registry: ToolAdapterRegistry,
        run_id: str,
        trace_id: str,
        step: ToolChainStepDefinition,
        step_state: ToolChainStepState,
        steps_output: dict[str, Any],
        memory: dict[str, Any],
        context: ToolChainContextState,
    ) -> dict[str, Any]:
        rollback_kind = step.rollback.kind
        step_output = (steps_output.get(step.step_id) or {}).get("output") or {}
        if rollback_kind == "none" and step.skill_name == "qtable.task.records.create":
            rollback_kind = "delete_created_records"

        if rollback_kind == "delete_created_records":
            created = step_output.get("createdRecords") or []
            table_id = step_output.get("tableId")
            record_ids = [
                item.get("recordId")
                for item in created
                if isinstance(item, dict) and item.get("recordId")
            ]
            if not table_id or not record_ids:
                return {"status": "skipped", "reason": "no_created_records"}
            return await self._delete_created_records(db=db, table_id=table_id, record_ids=record_ids)

        if rollback_kind == "skill" and step.rollback.skill_name:
            spec = adapter_registry.get_by_skill_name(step.rollback.skill_name)
            if spec is None:
                return {"status": "skipped", "reason": "rollback_skill_not_found"}
            source = self._build_resolution_source(
                request=request,
                context=context,
                memory=memory,
                steps_output=steps_output,
            )
            arguments = self._resolve_value(step.rollback.arguments, source)
            adapter_context = build_tool_adapter_context(
                execution_context=execution_context,
                skill_name=spec.skill_name,
                function_name=spec.function_name,
                runtime_context={
                    "traceId": trace_id,
                    "runId": run_id,
                    "mode": "tool_chain_rollback",
                },
                request_context={"arguments": arguments, "confirmed": True},
                state={
                    "rollbackForStepId": step.step_id,
                    "metadata": spec.manifest.metadata.model_dump(mode="json"),
                },
            )
            outcome = await tool_executor.execute(
                request=ToolExecutionRequest(
                    toolCallId=str(uuid.uuid4()),
                    skillName=spec.skill_name,
                    functionName=spec.function_name,
                    arguments=arguments,
                    confirmed=True,
                    dry_run=False,
                    traceId=trace_id,
                ),
                execution_context=execution_context,
                adapter_context=adapter_context,
                retry_config=ToolRetryConfig(
                    maxAttempts=max(1, request.retry.max_attempts),
                    backoffMs=request.retry.backoff_ms,
                ),
            )
            return {
                "status": outcome.response.state.value,
                "output": outcome.response.output,
                "error": outcome.response.error.model_dump(mode="json") if outcome.response.error else None,
            }

        return {"status": "skipped", "reason": "no_rollback_strategy"}

    async def _perform_rollback(
        self,
        *,
        db: AsyncSession,
        request: ToolChainRequest,
        run_row: ToolChainRun,
        plan: ToolChainPlan,
        step_states: dict[str, ToolChainStepState],
        step_rows: dict[str, ToolChainStepRun],
        completed_steps: list[str],
        steps_output: dict[str, Any],
        memory: dict[str, Any],
        context: ToolChainContextState,
        execution_context: SkillExecutionContext,
        runtime: SkillRuntime,
        tool_executor: ToolExecutor,
        adapter_registry: ToolAdapterRegistry,
        response_builder,
        event_sink,
    ) -> str:
        await self._emit_event(
            db=db,
            run_row=run_row,
            step_id=None,
            event_type="rollback_started",
            payload={"completedSteps": completed_steps},
            response_builder=response_builder,
            event_sink=event_sink,
        )
        run_row.status = "rolling_back"
        await db.commit()

        failed_rollbacks: list[dict[str, Any]] = []
        steps_by_id = {step.step_id: step for step in plan.steps}
        for step_id in reversed(completed_steps):
            step = steps_by_id[step_id]
            state = step_states[step_id]
            try:
                rollback_result = await self._rollback_step(
                    db=db,
                    request=request,
                    execution_context=execution_context,
                    runtime=runtime,
                    tool_executor=tool_executor,
                    adapter_registry=adapter_registry,
                    run_id=run_row.id,
                    trace_id=run_row.trace_id,
                    step=step,
                    step_state=state,
                    steps_output=steps_output,
                    memory=memory,
                    context=context,
                )
            except Exception as exc:
                rollback_result = {"status": "failed", "message": str(exc)}

            state.state = "rolled_back" if rollback_result.get("status") != "failed" else "failed"
            state.rollback = {**state.rollback, "result": rollback_result}
            step_rows[step_id].status = state.state
            step_rows[step_id].rollback_payload = state.rollback
            if rollback_result.get("status") == "failed":
                failed_rollbacks.append({"stepId": step_id, "result": rollback_result})
            await db.commit()

        final_status = "rolled_back" if not failed_rollbacks else "failed"
        await self._emit_event(
            db=db,
            run_row=run_row,
            step_id=None,
            event_type="rollback_completed",
            payload={"status": final_status, "failedRollbacks": failed_rollbacks},
            response_builder=response_builder,
            event_sink=event_sink,
        )
        return final_status

    def _build_response(
        self,
        *,
        run_row: ToolChainRun,
        request: ToolChainRequest,
        context: ToolChainContextState,
        plan: ToolChainPlan | None,
        step_states: dict[str, ToolChainStepState],
    ) -> ToolChainRunResponse:
        return ToolChainRunResponse(
            runId=run_row.id,
            traceId=run_row.trace_id,
            status=run_row.status,
            provider=run_row.provider,
            model=run_row.model,
            message=run_row.source_message,
            summary=run_row.summary or "",
            plan=ToolChainPlan.model_validate(run_row.plan_json) if run_row.plan_json else plan,
            context=ToolChainContextState.model_validate(run_row.context_json or context.model_dump(mode="json", by_alias=True)),
            steps=[
                step_states[key]
                for key in sorted(step_states.keys(), key=lambda item: next(
                    (
                        idx
                        for idx, step in enumerate((plan.steps if plan else []))
                        if step.step_id == item
                    ),
                    9999,
                ))
            ],
            memory=run_row.memory_json or {},
            result=run_row.result_json or {},
            pendingConfirmation=run_row.pending_confirmation,
            error=run_row.error_json,
            startedAt=run_row.started_at,
            finishedAt=run_row.finished_at,
        )

    async def _run_internal(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: ToolChainRequest,
        event_sink=None,
    ) -> ToolChainRunResponse:
        trace_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        context = self._normalize_context(request)

        runtime_registry = await skill_registry_service.build_runtime_registry(
            db,
            workspace_id=context.workspace_id,
        )
        manifests = runtime_registry.list_manifest()
        placeholder_run = await self._create_run_row(
            db=db,
            run_id=run_id,
            trace_id=trace_id,
            user_id=user_id,
            request=request,
            context=context,
            status="planning",
            plan=None,
            provider=None,
            model=request.model,
        )
        empty_step_states: dict[str, ToolChainStepState] = {}
        response_builder = lambda: self._build_response(
            run_row=placeholder_run,
            request=request,
            context=context,
            plan=None,
            step_states=empty_step_states,
        )
        await self._emit_event(
            db=db,
            run_row=placeholder_run,
            step_id=None,
            event_type="planning_started",
            payload={"message": request.message},
            response_builder=response_builder,
            event_sink=event_sink,
        )

        plan, provider, model_name = await self._plan_chain(
            db=db,
            user_id=user_id,
            request=request,
            manifests=manifests,
        )
        placeholder_run.provider = provider
        placeholder_run.model = model_name
        placeholder_run.plan_json = plan.model_dump(mode="json", by_alias=True)
        placeholder_run.status = "running"
        await db.commit()

        step_states = self._build_step_states(plan)
        step_rows = await self._sync_step_rows(
            db=db,
            run_id=run_id,
            plan=plan,
            step_states=step_states,
        )
        response_builder = lambda: self._build_response(
            run_row=placeholder_run,
            request=request,
            context=context,
            plan=plan,
            step_states=step_states,
        )
        await self._emit_event(
            db=db,
            run_row=placeholder_run,
            step_id=None,
            event_type="planning_completed",
            payload={"plan": plan.model_dump(mode="json", by_alias=True)},
            response_builder=response_builder,
            event_sink=event_sink,
        )

        runtime = SkillRuntime(runtime_registry)
        adapter_registry = ToolAdapterRegistry.from_runtime_registry(runtime_registry)
        tool_executor = self._build_tool_executor(runtime)
        execution_context = self._build_execution_context(
            user_id=user_id,
            request=request,
            context=context,
            trace_id=trace_id,
            db=db,
        )

        memory = dict(context.memory)
        steps_output: dict[str, Any] = {}
        completed_steps: list[str] = []
        pending_confirmation: dict[str, Any] | None = None

        await self._emit_event(
            db=db,
            run_row=placeholder_run,
            step_id=None,
            event_type="run_started",
            payload={"runId": run_id, "traceId": trace_id},
            response_builder=response_builder,
            event_sink=event_sink,
        )

        for step in plan.steps:
            unresolved_deps = [
                dependency
                for dependency in step.depends_on
                if step_states.get(dependency) is None
                or step_states[dependency].state not in {"completed", "rolled_back"}
            ]
            if unresolved_deps:
                step_states[step.step_id].state = "skipped"
                step_rows[step.step_id].status = "skipped"
                step_rows[step.step_id].error_json = {
                    "code": "DEPENDENCY_NOT_READY",
                    "message": f"Dependencies not ready: {unresolved_deps}",
                }
                await db.commit()
                continue

            step_state = step_states[step.step_id]
            step_row = step_rows[step.step_id]
            step_state.state = "running"
            step_state.started_at = _now_utc()
            step_row.status = "running"
            step_row.started_at = step_state.started_at
            placeholder_run.current_step_id = step.step_id
            await db.commit()

            source = self._build_resolution_source(
                request=request,
                context=context,
                memory=memory,
                steps_output=steps_output,
            )
            resolved_arguments = self._resolve_value(step.arguments, source) or {}
            if not isinstance(resolved_arguments, dict):
                resolved_arguments = {"value": resolved_arguments}
            step_state.input_payload = resolved_arguments
            step_row.input_payload = resolved_arguments

            await self._emit_event(
                db=db,
                run_row=placeholder_run,
                step_id=step.step_id,
                event_type="step_started",
                payload={
                    "stepId": step.step_id,
                    "skillName": step.skill_name,
                    "arguments": resolved_arguments,
                },
                response_builder=response_builder,
                event_sink=event_sink,
            )

            spec = adapter_registry.get_by_skill_name(step.skill_name)
            if spec is None:
                error = {"code": "SKILL_NOT_FOUND", "message": f"Skill not found: {step.skill_name}"}
                step_state.state = "failed"
                step_state.error = error
                step_state.finished_at = _now_utc()
                step_row.status = "failed"
                step_row.error_json = error
                step_row.finished_at = step_state.finished_at
                placeholder_run.status = "failed"
                placeholder_run.error_json = error
                await db.commit()
                await self._emit_event(
                    db=db,
                    run_row=placeholder_run,
                    step_id=step.step_id,
                    event_type="step_failed",
                    payload=error,
                    response_builder=response_builder,
                    event_sink=event_sink,
                )
                break

            adapter_context = build_tool_adapter_context(
                execution_context=execution_context,
                skill_name=spec.skill_name,
                function_name=spec.function_name,
                runtime_context={
                    "traceId": trace_id,
                    "runId": run_id,
                    "stepId": step.step_id,
                    "mode": "tool_chain",
                    "workspaceId": context.workspace_id,
                    "tableIds": context.table_ids,
                },
                request_context={"arguments": resolved_arguments, "confirmed": request.confirmed},
                state={
                    "step": step.model_dump(mode="json", by_alias=True),
                    "metadata": spec.manifest.metadata.model_dump(mode="json"),
                    "schemas": spec.schema_bundle.model_dump(mode="json", by_alias=True),
                },
            )
            retry_config = ToolRetryConfig.model_validate(step.retry.model_dump(mode="json", by_alias=True))
            try:
                outcome = await asyncio.wait_for(
                    tool_executor.execute(
                        request=ToolExecutionRequest(
                            toolCallId=str(uuid.uuid4()),
                            skillName=step.skill_name,
                            functionName=spec.function_name,
                            arguments=resolved_arguments,
                            confirmed=request.confirmed,
                            dry_run=request.dry_run,
                            traceId=trace_id,
                        ),
                        execution_context=execution_context,
                        adapter_context=adapter_context,
                        retry_config=retry_config,
                    ),
                    timeout=step.timeout_seconds,
                )
            except asyncio.TimeoutError:
                error = {"code": "STEP_TIMEOUT", "message": f"Step timed out after {step.timeout_seconds}s"}
                step_state.state = "failed"
                step_state.error = error
                step_state.finished_at = _now_utc()
                step_row.status = "failed"
                step_row.error_json = error
                step_row.finished_at = step_state.finished_at
                placeholder_run.status = "failed"
                placeholder_run.error_json = error
                await db.commit()
                await self._emit_event(
                    db=db,
                    run_row=placeholder_run,
                    step_id=step.step_id,
                    event_type="step_failed",
                    payload=error,
                    response_builder=response_builder,
                    event_sink=event_sink,
                )
                break

            response = outcome.response
            step_state.attempt_count = outcome.attempt_count
            step_row.attempt_count = outcome.attempt_count

            if outcome.attempt_count > 1:
                await self._emit_event(
                    db=db,
                    run_row=placeholder_run,
                    step_id=step.step_id,
                    event_type="step_retrying",
                    payload={"attemptCount": outcome.attempt_count},
                    response_builder=response_builder,
                    event_sink=event_sink,
                )

            if response.state.value == "requires_confirmation":
                pending_confirmation = {
                    "stepId": step.step_id,
                    "skillName": step.skill_name,
                    "arguments": resolved_arguments,
                    "preview": response.metadata,
                }
                step_state.state = "waiting_confirmation"
                step_state.output_payload = response.metadata
                step_state.finished_at = _now_utc()
                step_row.status = "waiting_confirmation"
                step_row.output_payload = response.metadata
                step_row.finished_at = step_state.finished_at
                placeholder_run.status = "waiting_confirmation"
                placeholder_run.pending_confirmation = pending_confirmation
                placeholder_run.context_json = {
                    **context.model_dump(mode="json", by_alias=True),
                    "memory": memory,
                }
                placeholder_run.memory_json = memory
                await db.commit()
                await self._emit_event(
                    db=db,
                    run_row=placeholder_run,
                    step_id=step.step_id,
                    event_type="step_waiting_confirmation",
                    payload=pending_confirmation,
                    response_builder=response_builder,
                    event_sink=event_sink,
                )
                break

            if response.error:
                error = response.error.model_dump(mode="json")
                step_state.state = "failed"
                step_state.error = error
                step_state.output_payload = response.output
                step_state.finished_at = _now_utc()
                step_row.status = "failed"
                step_row.error_json = error
                step_row.output_payload = response.output
                step_row.finished_at = step_state.finished_at
                placeholder_run.status = "failed"
                placeholder_run.error_json = error
                placeholder_run.context_json = {
                    **context.model_dump(mode="json", by_alias=True),
                    "memory": memory,
                }
                placeholder_run.memory_json = memory
                await db.commit()
                await self._emit_event(
                    db=db,
                    run_row=placeholder_run,
                    step_id=step.step_id,
                    event_type="step_failed",
                    payload=error,
                    response_builder=response_builder,
                    event_sink=event_sink,
                )
                break

            payload = {
                "output": response.output or {},
                "metadata": response.metadata,
            }
            steps_output[step.step_id] = payload
            memory[step.step_id] = payload["output"]
            if step.save_result_as:
                memory[step.save_result_as] = payload["output"]
            context.tool_results.append(
                {
                    "stepId": step.step_id,
                    "skillName": step.skill_name,
                    "summary": f"{step.skill_name} completed",
                    "output": response.output,
                }
            )
            step_state.state = "completed"
            step_state.output_payload = response.output
            step_state.finished_at = _now_utc()
            step_row.status = "completed"
            step_row.output_payload = response.output
            step_row.context_snapshot = {
                "memory": memory,
                "toolResults": context.tool_results[-10:],
            }
            step_row.finished_at = step_state.finished_at
            placeholder_run.context_json = {
                **context.model_dump(mode="json", by_alias=True),
                "memory": memory,
            }
            placeholder_run.memory_json = memory
            await db.commit()
            completed_steps.append(step.step_id)
            await self._emit_event(
                db=db,
                run_row=placeholder_run,
                step_id=step.step_id,
                event_type="step_completed",
                payload={
                    "output": response.output,
                    "attemptCount": outcome.attempt_count,
                },
                response_builder=response_builder,
                event_sink=event_sink,
            )

        if placeholder_run.status == "failed" and completed_steps and request.rollback.enabled:
            placeholder_run.status = await self._perform_rollback(
                db=db,
                request=request,
                run_row=placeholder_run,
                plan=plan,
                step_states=step_states,
                step_rows=step_rows,
                completed_steps=completed_steps,
                steps_output=steps_output,
                memory=memory,
                context=context,
                execution_context=execution_context,
                runtime=runtime,
                tool_executor=tool_executor,
                adapter_registry=adapter_registry,
                response_builder=response_builder,
                event_sink=event_sink,
            )

        if placeholder_run.status == "running":
            placeholder_run.status = "completed"

        final_result = self._collect_final_result(
            request=request,
            plan=plan,
            memory=memory,
            steps_output=steps_output,
        )
        placeholder_run.result_json = final_result if isinstance(final_result, dict) else {"value": final_result}
        placeholder_run.memory_json = memory
        placeholder_run.context_json = {
            **context.model_dump(mode="json", by_alias=True),
            "memory": memory,
        }
        placeholder_run.summary = plan.summary
        placeholder_run.finished_at = _now_utc()
        await db.commit()

        final_event_type = "run_completed" if placeholder_run.status in {"completed", "rolled_back"} else "run_failed"
        await self._emit_event(
            db=db,
            run_row=placeholder_run,
            step_id=None,
            event_type=final_event_type,
            payload={
                "status": placeholder_run.status,
                "result": placeholder_run.result_json,
                "pendingConfirmation": pending_confirmation,
            },
            response_builder=response_builder,
            event_sink=event_sink,
        )
        return response_builder()

    async def run(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: ToolChainRequest,
    ) -> ToolChainRunResponse:
        return await self._run_internal(db=db, user_id=user_id, request=request)

    async def stream(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: ToolChainRequest,
    ) -> AsyncIterator[ToolChainEvent]:
        queue: asyncio.Queue[ToolChainEvent | None] = asyncio.Queue()

        async def sink(event: ToolChainEvent) -> None:
            await queue.put(event)

        async def run_task() -> None:
            try:
                await self._run_internal(
                    db=db,
                    user_id=user_id,
                    request=request,
                    event_sink=sink,
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(run_task())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            await task

    async def get_run(
        self,
        *,
        db: AsyncSession,
        run_id: str,
    ) -> ToolChainRunResponse | None:
        result = await db.execute(select(ToolChainRun).where(ToolChainRun.id == run_id))
        run_row = result.scalars().first()
        if run_row is None:
            return None
        step_result = await db.execute(
            select(ToolChainStepRun)
            .where(ToolChainStepRun.run_id == run_id)
            .order_by(ToolChainStepRun.order_index.asc())
        )
        step_rows = step_result.scalars().all()
        plan = ToolChainPlan.model_validate(run_row.plan_json) if run_row.plan_json else None
        step_states: dict[str, ToolChainStepState] = {}
        for row in step_rows:
            step_states[row.step_id] = ToolChainStepState(
                stepId=row.step_id,
                skillName=row.skill_name,
                description=row.description or "",
                dependsOn=row.depends_on or [],
                state=row.status,
                attemptCount=row.attempt_count,
                inputPayload=row.input_payload or {},
                outputPayload=row.output_payload,
                error=row.error_json,
                rollback=row.rollback_payload or {},
                startedAt=row.started_at,
                finishedAt=row.finished_at,
            )
        return self._build_response(
            run_row=run_row,
            request=ToolChainRequest.model_validate(run_row.request_payload),
            context=ToolChainContextState.model_validate(run_row.context_json or {}),
            plan=plan,
            step_states=step_states,
        )


tool_chain_runtime_service = ToolChainRuntimeService()
