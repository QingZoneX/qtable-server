from __future__ import annotations

import asyncio
import time
from typing import Sequence

from app.skills.contracts import SkillCallRequest, SkillErrorCode, SkillSideEffect
from app.skills.runtime import SkillExecutionContext, SkillRuntime
from app.tool_adapter.contracts import (
    ToolAdapterContext,
    ToolExecutionOutcome,
    ToolExecutionRequest,
    ToolLifecycleEvent,
    ToolLifecycleHook,
    ToolLifecycleStage,
    ToolMiddleware,
    ToolRetryConfig,
)


class ToolExecutor:
    def __init__(
        self,
        runtime: SkillRuntime,
        *,
        middlewares: Sequence[ToolMiddleware] | None = None,
        hooks: Sequence[ToolLifecycleHook] | None = None,
    ) -> None:
        self.runtime = runtime
        self.middlewares = list(middlewares or [])
        self.hooks = list(hooks or [])

    async def execute(
        self,
        request: ToolExecutionRequest,
        execution_context: SkillExecutionContext,
        adapter_context: ToolAdapterContext,
        retry_config: ToolRetryConfig | None = None,
    ) -> ToolExecutionOutcome:
        retry_config = retry_config or ToolRetryConfig()
        response = None
        duration_ms = 0.0

        for attempt in range(1, retry_config.max_attempts + 1):
            started = time.perf_counter()
            await self._dispatch(
                ToolLifecycleStage.BEFORE_EXECUTE,
                request,
                adapter_context,
                execution_context,
                attempt=attempt,
            )
            try:
                response = await self._invoke_with_middlewares(request, execution_context, adapter_context)
            except Exception as exc:
                duration_ms = (time.perf_counter() - started) * 1000
                adapter_context.metrics["durationMs"] = round(duration_ms, 3)
                await self._dispatch(
                    ToolLifecycleStage.FAILED,
                    request,
                    adapter_context,
                    execution_context,
                    attempt=attempt,
                    error=exc,
                    duration_ms=duration_ms,
                )
                raise

            duration_ms = (time.perf_counter() - started) * 1000
            adapter_context.metrics["durationMs"] = round(duration_ms, 3)
            adapter_context.metrics["attemptCount"] = attempt

            if response.state.value == "requires_confirmation":
                await self._dispatch(
                    ToolLifecycleStage.REQUIRES_CONFIRMATION,
                    request,
                    adapter_context,
                    execution_context,
                    attempt=attempt,
                    response=response,
                    duration_ms=duration_ms,
                )
                break

            if response.error and self._should_retry(response, attempt, retry_config, adapter_context):
                await self._dispatch(
                    ToolLifecycleStage.RETRY,
                    request,
                    adapter_context,
                    execution_context,
                    attempt=attempt,
                    response=response,
                    duration_ms=duration_ms,
                    metadata={"code": response.error.code.value},
                )
                if retry_config.backoff_ms > 0:
                    await asyncio.sleep(retry_config.backoff_ms / 1000)
                continue

            await self._dispatch(
                ToolLifecycleStage.AFTER_EXECUTE,
                request,
                adapter_context,
                execution_context,
                attempt=attempt,
                response=response,
                duration_ms=duration_ms,
            )
            break

        if response is None:
            raise RuntimeError("Tool executor did not produce a response")

        return ToolExecutionOutcome(
            request=request,
            response=response,
            adapter_context=adapter_context,
            attempt_count=int(adapter_context.metrics.get("attemptCount") or 1),
            duration_ms=duration_ms,
        )

    async def _invoke_with_middlewares(
        self,
        request: ToolExecutionRequest,
        execution_context: SkillExecutionContext,
        adapter_context: ToolAdapterContext,
    ):
        async def call_runtime(
            current_request: ToolExecutionRequest,
            current_context: ToolAdapterContext,
        ):
            return await self.runtime.invoke(
                SkillCallRequest(
                    call_id=current_request.tool_call_id,
                    skill_name=current_request.skill_name,
                    input=current_request.arguments,
                    dry_run=current_request.dry_run,
                    confirmed=current_request.confirmed,
                    origin=execution_context.context.origin,
                    trace_id=current_request.trace_id,
                ),
                execution_context,
            )

        call_next = call_runtime
        for middleware in reversed(self.middlewares):
            next_handler = call_next

            async def wrapper(
                current_request,
                current_context,
                middleware=middleware,
                next_handler=next_handler,
            ):
                return await middleware(current_request, current_context, next_handler)

            call_next = wrapper

        return await call_next(request, adapter_context)

    def _should_retry(
        self,
        response,
        attempt: int,
        retry_config: ToolRetryConfig,
        adapter_context: ToolAdapterContext,
    ) -> bool:
        if attempt >= retry_config.max_attempts:
            return False
        if not response.error:
            return False
        if response.error.code not in {SkillErrorCode.INVALID_INPUT, SkillErrorCode.EXECUTION_FAILED}:
            return False
        metadata = ((adapter_context.state or {}).get("metadata") or {})
        side_effect = metadata.get("side_effect", SkillSideEffect.NONE.value)
        if response.metadata.get("confirmed") and side_effect != SkillSideEffect.NONE.value:
            if not bool(metadata.get("idempotent", False)):
                return False
        return True

    async def _dispatch(
        self,
        stage: ToolLifecycleStage,
        request: ToolExecutionRequest,
        adapter_context: ToolAdapterContext,
        execution_context: SkillExecutionContext,
        *,
        attempt: int,
        response=None,
        error: Exception | None = None,
        duration_ms: float | None = None,
        metadata: dict | None = None,
    ) -> None:
        event = ToolLifecycleEvent(
            stage=stage,
            request=request,
            adapter_context=adapter_context,
            execution_context=execution_context,
            attempt=attempt,
            response=response,
            error=error,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        for hook in self.hooks:
            await hook.on_event(event)
