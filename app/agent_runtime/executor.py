from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.state import AgentStateMachine
from app.agent_runtime.types import (
    ActionPreview,
    AgentState,
    RuntimeContext,
    ToolPlan,
    ToolPlanEngine,
    ToolPlanStep,
    ToolPlanStepStatus,
    ToolCallRecord,
    ToolCallState,
)
from app.agent_runtime.planner import get_planner
from app.agent_runtime.intent_router import get_intent_router
from app.agent_runtime.observer import get_observer
from app.agent_runtime.preview_manager import get_preview_manager
from app.agent_runtime.confirm_manager import get_confirm_manager
from app.agent_runtime.sse_bridge import SSEEventEmitter
from app.services.skill_registry import skill_registry_service
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
)

logger = logging.getLogger(__name__)


class AgentRuntimeExecutor:
    def __init__(self) -> None:
        self._state_machine = AgentStateMachine()
        self._executors: dict[int, ToolExecutor] = {}

    @property
    def state(self) -> AgentState:
        return self._state_machine.state

    def reset(self) -> None:
        self._state_machine.reset()

    async def run(
        self,
        ctx: RuntimeContext,
        db: AsyncSession,
        user_id: int,
        model: Any,
        emitter: SSEEventEmitter,
    ) -> None:
        try:
            self._state_machine.transition("start")
            emitter.emit_state(self._state_machine.state)

            plan = await self._plan(ctx, model, emitter)
            if plan is None:
                return

            needs_preview = plan.requires_confirmation and any(
                s.require_approval for s in plan.steps
            )

            if not plan.steps:
                self._state_machine.transition("direct_answer")
                emitter.emit_state(self._state_machine.state)
                await self._run_chat_answer(ctx, model, db, user_id, emitter)
                self._state_machine.transition("all_complete")
                emitter.emit_state(self._state_machine.state)
                return

            if needs_preview:
                self._state_machine.transition("need_preview")
                emitter.emit_state(self._state_machine.state)

                previews = await self._generate_previews(plan, db, user_id, ctx)
                for preview in previews:
                    emitter.emit_preview(preview)

                has_write = any(
                    s.require_approval for s in plan.steps
                    if s.status != ToolPlanStepStatus.COMPLETED
                )
                if has_write:
                    confirm_manager = get_confirm_manager()
                    first_approval_step = next(
                        (s for s in plan.steps if s.require_approval), None
                    )
                    if first_approval_step and previews:
                        confirm = await confirm_manager.create_confirm(
                            step_id=first_approval_step.step_id,
                            preview=previews[0],
                            timeout_ms=ctx.config.confirm_timeout_ms,
                        )
                        self._state_machine.transition("show_preview")
                        emitter.emit_state(self._state_machine.state)
                        emitter.emit_confirm_required(
                            confirm_id=confirm.confirm_id,
                            timeout=confirm.timeout_ms,
                        )
                        return

                self._state_machine.transition("no_confirm_needed")
                emitter.emit_state(self._state_machine.state)

            await self._execute_plan(plan, ctx, db, user_id, emitter)

            self._state_machine.transition("all_complete")
            emitter.emit_state(self._state_machine.state)

        except Exception as exc:
            logger.exception("AgentRuntimeExecutor run failed")
            try:
                self._state_machine.transition("failed")
            except ValueError:
                pass
            emitter.emit_state(self._state_machine.state)
            emitter.emit_error(str(exc), "RUNTIME_ERROR")

    async def resume_after_confirm(
        self,
        ctx: RuntimeContext,
        db: AsyncSession,
        user_id: int,
        confirm_id: str,
        approved: bool,
        emitter: SSEEventEmitter,
    ) -> None:
        confirm_manager = get_confirm_manager()

        try:
            confirm = await confirm_manager.resolve(confirm_id, approved)
        except ValueError:
            emitter.emit_error(f"Confirm not found: {confirm_id}", "CONFIRM_NOT_FOUND")
            return

        if not approved:
            self._state_machine.transition("rejected")
            emitter.emit_state(self._state_machine.state)
            emitter.emit_text("操作已取消。")
            emitter.emit_done()
            return

        self._state_machine.transition("approved")
        emitter.emit_state(self._state_machine.state)

        plan = getattr(ctx, "_plan", None)
        if plan is None:
            emitter.emit_text("执行计划已过期，请重新发起请求。")
            emitter.emit_done()
            return

        for step in plan.steps:
            if step.step_id == confirm.step_id:
                step.status = ToolPlanStepStatus.PENDING
                step.require_approval = False

        model = self._build_model(ctx)
        await self._execute_plan(plan, ctx, db, user_id, emitter)

        self._state_machine.transition("all_complete")
        emitter.emit_state(self._state_machine.state)

    async def _plan(
        self,
        ctx: RuntimeContext,
        model: Any,
        emitter: SSEEventEmitter,
    ) -> Optional[ToolPlan]:
        intent_router = get_intent_router()
        intent = await intent_router.route(
            message=ctx.user_message,
            model=model,
            context={
                "workspace_id": ctx.workspace_id,
                "table_ids": ctx.table_ids,
                "available_skills": ctx.allowed_skills,
            },
        )
        emitter.emit_intent(intent)

        if intent.intent.value == "chat":
            return ToolPlan(
                goal=ctx.user_message,
                intent="chat",
                intent_confidence=intent.confidence,
                steps=[],
            )

        planner = get_planner()
        plan = await planner.plan(
            message=ctx.user_message,
            intent=intent,
            model=model,
            context={
                "workspace_id": ctx.workspace_id,
                "table_ids": ctx.table_ids,
                "available_skills": ctx.allowed_skills,
                "denied_skills": ctx.denied_skills,
            },
        )
        emitter.emit_plan(plan)

        setattr(ctx, "_plan", plan)
        return plan

    async def _run_chat_answer(
        self,
        ctx: RuntimeContext,
        model: Any,
        db: AsyncSession,
        user_id: int,
        emitter: SSEEventEmitter,
    ) -> None:
        from app.services.ai_service import analyze_table_data
        from app.services.encryption import decrypt_api_key
        from app.models.ai_config import AiConfig
        from sqlalchemy import select

        config_result = await db.execute(
            select(AiConfig).where(AiConfig.user_id == str(user_id))
        )
        config = config_result.scalars().first()
        if not config:
            emitter.emit_error("AI configuration not found", "AI_CONFIG_NOT_FOUND")
            return

        api_key = decrypt_api_key(config.api_key_encrypted)
        messages = ctx.messages or []

        from pydantic_ai.models.openai import OpenAIChatModel as PydanticOpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        if hasattr(model, 'model_name'):
            model_name = model.model_name
        else:
            model_name = "deepseek-chat"

        openai_model = PydanticOpenAIChatModel(
            model_name=model_name,
            provider=OpenAIProvider(api_key=api_key, base_url="https://api.deepseek.com/v1"),
        )

        from pydantic_ai import Agent as PydanticAgent
        agent = PydanticAgent(
            model=openai_model,
            system_prompt="你是 QTable AI 助手，请简洁准确地回答用户问题。",
        )

        full_content = ""
        async with agent.run_stream(ctx.user_message, message_history=messages) as stream:
            async for chunk in stream.stream_text(delta=True):
                if chunk:
                    full_content += chunk
                    emitter.emit_text(chunk)

        emitter.emit_done()

    async def _generate_previews(
        self,
        plan: ToolPlan,
        db: AsyncSession,
        user_id: int,
        ctx: RuntimeContext,
    ) -> list:
        preview_manager = get_preview_manager()
        previews = []

        for step in plan.steps:
            if not step.require_approval:
                continue
            if not step.skill_name:
                continue

            preview = ActionPreview(
                step_id=step.step_id,
                skill_name=step.skill_name,
                action="update_record",
                summary=step.description or f"步骤 {step.index}: {step.skill_name}",
                affected_count=0,
                record_changes=[],
                table_id=ctx.table_ids[0] if ctx.table_ids else "",
                raw_arguments=step.arguments,
            )
            previews.append(preview)

        return previews

    async def _execute_plan(
        self,
        plan: ToolPlan,
        ctx: RuntimeContext,
        db: AsyncSession,
        user_id: int,
        emitter: SSEEventEmitter,
    ) -> None:
        engine = ToolPlanEngine()
        batches = engine.get_execution_order(plan)

        for batch in batches:
            self._state_machine.transition("step_complete" if self._state_machine.state == AgentState.OBSERVING else "direct_execute")
            emitter.emit_state(self._state_machine.state)

            tasks = []
            for step in batch:
                if step.status in {ToolPlanStepStatus.SKIPPED, ToolPlanStepStatus.COMPLETED}:
                    continue
                tasks.append(self._execute_step(step, ctx, db, user_id, emitter))

            if tasks:
                await asyncio.gather(*tasks)

            if self._state_machine.state == AgentState.STOPPED:
                return

            for step in batch:
                if step.status == ToolPlanStepStatus.FAILED and step.retry_count < step.max_retries:
                    step.retry_count += 1
                    step.status = ToolPlanStepStatus.PENDING
                    step.error = None
                    logger.info("Retrying step=%s attempt=%d", step.step_id, step.retry_count)
                    await self._execute_step(step, ctx, db, user_id, emitter)

            for step in batch:
                if step.status == ToolPlanStepStatus.REQUIRES_APPROVAL:
                    return

            observer = get_observer()
            for step in batch:
                if step.status == ToolPlanStepStatus.COMPLETED or step.status == ToolPlanStepStatus.FAILED:
                    tc = self._build_tool_call_record(step)
                    obs = observer.observe_tool_call(tc, step)
                    emitter.emit_observation(obs)

    async def _execute_step(
        self,
        step: ToolPlanStep,
        ctx: RuntimeContext,
        db: AsyncSession,
        user_id: int,
        emitter: SSEEventEmitter,
    ) -> None:
        step.status = ToolPlanStepStatus.RUNNING
        step.started_at = datetime.now(timezone.utc).isoformat()

        tc = ToolCallRecord(
            step_id=step.step_id,
            skill_name=step.skill_name or "unknown",
            function_name=step.function_name or step.skill_name or "unknown",
            arguments=step.arguments,
            state=ToolCallState.RUNNING,
        )

        emitter.emit_tool_start(tc.tool_call_id, tc.skill_name, tc.step_id)

        try:
            registry = await skill_registry_service.build_runtime_registry(
                db,
                workspace_id=ctx.workspace_id,
            )
            runtime = SkillRuntime(registry)

            executor = self._get_or_create_executor(runtime, db)

            adapter_registry = ToolAdapterRegistry.from_runtime_registry(
                registry,
                skill_names=[step.skill_name] if step.skill_name else None,
            )

            spec = adapter_registry.get_by_skill_name(step.skill_name or "")
            if spec is None:
                raise RuntimeError(f"Skill not found: {step.skill_name}")

            execution_context = SkillExecutionContext(
                context=SkillExecutionContext.model_fields.get("context", type(None)),
                dry_run=False,
                confirmed=False,
            )
            execution_context.context.user_id = user_id
            execution_context.context.workspace_id = ctx.workspace_id

            request = ToolExecutionRequest(
                skill_name=step.skill_name or "",
                function_name=step.function_name or step.skill_name or "",
                arguments=step.arguments,
            )

            result = await executor.execute(
                request,
                execution_context,
                adapter_registry,
            )

            tc.state = ToolCallState.COMPLETED
            tc.output = result.output if hasattr(result, 'output') else {}
            tc.duration_ms = result.duration_ms if hasattr(result, 'duration_ms') else 0

            step.status = ToolPlanStepStatus.COMPLETED
            step.result = tc.output

        except Exception as exc:
            logger.exception("Step execution failed step=%s", step.step_id)
            tc.state = ToolCallState.FAILED
            tc.error = {"code": "EXECUTION_FAILED", "message": str(exc)}
            step.status = ToolPlanStepStatus.FAILED
            step.error = tc.error

        step.completed_at = datetime.now(timezone.utc).isoformat()
        emitter.emit_tool_end(
            tc.tool_call_id,
            tc.state.value,
            tc.duration_ms,
        )

    def _build_tool_call_record(self, step: ToolPlanStep) -> ToolCallRecord:
        state = ToolCallState.COMPLETED if step.status == ToolPlanStepStatus.COMPLETED else ToolCallState.FAILED
        return ToolCallRecord(
            step_id=step.step_id,
            skill_name=step.skill_name or "unknown",
            function_name=step.function_name or step.skill_name or "unknown",
            arguments=step.arguments,
            state=state,
            output=step.result,
            error=step.error,
            attempt_count=step.retry_count + 1,
            duration_ms=0,
        )

    def _get_or_create_executor(self, runtime: SkillRuntime, db: AsyncSession) -> ToolExecutor:
        cache_key = id(runtime)
        if cache_key not in self._executors:
            metrics_sink = InMemoryToolMetricsSink()
            self._executors[cache_key] = ToolExecutor(
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
        return self._executors[cache_key]

    def _build_model(self, ctx: RuntimeContext) -> Any:
        from pydantic_ai.models.openai import OpenAIChatModel as PydanticOpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        from app.services.encryption import decrypt_api_key

        return PydanticOpenAIChatModel(
            model_name="deepseek-chat",
            provider=OpenAIProvider(api_key="", base_url="https://api.deepseek.com/v1"),
        )


_runtime_executor: AgentRuntimeExecutor | None = None


def get_runtime_executor() -> AgentRuntimeExecutor:
    global _runtime_executor
    if _runtime_executor is None:
        _runtime_executor = AgentRuntimeExecutor()
    return _runtime_executor
