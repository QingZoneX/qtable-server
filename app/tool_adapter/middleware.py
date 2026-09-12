from __future__ import annotations

import logging
import time

from app.skills.contracts import SkillCallResponse
from app.tool_adapter.contracts import (
    ToolAdapterContext,
    ToolExecutionRequest,
    ToolMetricsSink,
    ToolMiddlewareNext,
)
from app.tool_adapter.hooks import NoOpToolMetricsSink


class LoggingToolMiddleware:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)

    async def __call__(
        self,
        request: ToolExecutionRequest,
        adapter_context: ToolAdapterContext,
        call_next: ToolMiddlewareNext,
    ) -> SkillCallResponse:
        self.logger.info(
            "tool_adapter execute start trace_id=%s tool_call_id=%s skill=%s function=%s",
            request.trace_id,
            request.tool_call_id,
            request.skill_name,
            request.function_name,
        )
        try:
            response = await call_next(request, adapter_context)
        except Exception:
            self.logger.exception(
                "tool_adapter execute error trace_id=%s tool_call_id=%s skill=%s",
                request.trace_id,
                request.tool_call_id,
                request.skill_name,
            )
            raise
        self.logger.info(
            "tool_adapter execute finish trace_id=%s tool_call_id=%s skill=%s state=%s",
            request.trace_id,
            request.tool_call_id,
            request.skill_name,
            response.state.value,
        )
        return response


class MetricsToolMiddleware:
    def __init__(self, sink: ToolMetricsSink | None = None) -> None:
        self.sink = sink or NoOpToolMetricsSink()

    async def __call__(
        self,
        request: ToolExecutionRequest,
        adapter_context: ToolAdapterContext,
        call_next: ToolMiddlewareNext,
    ) -> SkillCallResponse:
        started = time.perf_counter()
        tags = {
            "skill": request.skill_name,
            "function": request.function_name,
        }
        try:
            response = await call_next(request, adapter_context)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            adapter_context.metrics["durationMs"] = round(duration_ms, 3)
            await self.sink.increment("tool.execute.error", tags=tags)
            await self.sink.observe(
                "tool.execute.duration_ms",
                duration_ms,
                tags={**tags, "state": "exception"},
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        adapter_context.metrics["durationMs"] = round(duration_ms, 3)
        await self.sink.increment("tool.execute.count", tags={**tags, "state": response.state.value})
        await self.sink.observe(
            "tool.execute.duration_ms",
            duration_ms,
            tags={**tags, "state": response.state.value},
        )
        return response
