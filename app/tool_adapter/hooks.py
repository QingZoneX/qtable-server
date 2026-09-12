from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import redis.asyncio as redis

from app.tool_adapter.contracts import ToolLifecycleEvent, ToolLifecycleHook, ToolMetricsSink


def _metric_key(metric: str, tags: dict[str, str] | None = None) -> str:
    if not tags:
        return metric
    ordered = ",".join(f"{key}={value}" for key, value in sorted(tags.items()))
    return f"{metric}|{ordered}"


class NoOpToolMetricsSink(ToolMetricsSink):
    async def increment(
        self,
        metric: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        return

    async def observe(
        self,
        metric: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        return


class InMemoryToolMetricsSink(ToolMetricsSink):
    def __init__(self) -> None:
        self.counters: dict[str, int] = defaultdict(int)
        self.observations: dict[str, list[float]] = defaultdict(list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "observations": {key: list(values[-20:]) for key, values in self.observations.items()},
        }

    async def increment(
        self,
        metric: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.counters[_metric_key(metric, tags)] += value

    async def observe(
        self,
        metric: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.observations[_metric_key(metric, tags)].append(value)


class RedisToolMetricsSink(ToolMetricsSink):
    def __init__(self, client: redis.Redis, prefix: str = "tool-metrics") -> None:
        self.client = client
        self.prefix = prefix

    async def increment(
        self,
        metric: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        key = f"{self.prefix}:counter:{_metric_key(metric, tags)}"
        await self.client.incrby(key, value)

    async def observe(
        self,
        metric: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        key = f"{self.prefix}:hist:{_metric_key(metric, tags)}"
        await self.client.rpush(key, value)
        await self.client.ltrim(key, -500, -1)


class LoggingLifecycleHook(ToolLifecycleHook):
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)

    async def on_event(self, event: ToolLifecycleEvent) -> None:
        self.logger.info(
            "tool_adapter lifecycle stage=%s trace_id=%s tool_call_id=%s skill=%s attempt=%s duration_ms=%s metadata=%s",
            event.stage.value,
            event.request.trace_id,
            event.request.tool_call_id,
            event.request.skill_name,
            event.attempt,
            None if event.duration_ms is None else round(event.duration_ms, 3),
            event.metadata,
        )


class MetricsLifecycleHook(ToolLifecycleHook):
    def __init__(self, sink: ToolMetricsSink | None = None) -> None:
        self.sink = sink or NoOpToolMetricsSink()

    async def on_event(self, event: ToolLifecycleEvent) -> None:
        tags = {
            "skill": event.request.skill_name,
            "stage": event.stage.value,
        }
        await self.sink.increment("tool.lifecycle.event", tags=tags)
        if event.duration_ms is not None:
            await self.sink.observe("tool.lifecycle.duration_ms", event.duration_ms, tags=tags)
