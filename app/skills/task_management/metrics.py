"""
Task Management - Tool Metrics

工具指标收集和上报，支持:
1. 内存指标收集 (InMemory)
2. Redis 指标持久化
3. Prometheus 格式导出 (可选)
4. 聚合指标查询
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from app.tool_adapter.contracts import ToolMetricsSink
from app.skills.task_management.contracts import TaskManagementToolLayer

logger = logging.getLogger(__name__)


class TaskManagementMetricsSink:
    """
    任务管理工具专用指标收集器。

    指标维度:
    - tool_name: 工具名称
    - layer: 工具分层
    - status: success / failed
    - cache_hit: True / False
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._gauges: dict[str, float] = defaultdict(float)
        self._tool_metrics: list[dict[str, Any]] = []

    async def increment(
        self,
        metric: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        tag_str = _build_tag_suffix(tags)
        key = f"{metric}{tag_str}"
        self._counters[key] += value

    async def observe(
        self,
        metric: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        tag_str = _build_tag_suffix(tags)
        key = f"{metric}{tag_str}"
        self._histograms[key].append(value)

    async def set_gauge(
        self,
        metric: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        tag_str = _build_tag_suffix(tags)
        self._gauges[f"{metric}{tag_str}"] = value

    def record_tool_call(
        self,
        tool_name: str,
        layer: TaskManagementToolLayer,
        success: bool,
        duration_ms: float,
        *,
        cache_hit: bool = False,
        retry_count: int = 0,
        records_processed: int = 0,
    ) -> None:
        """记录单次工具调用指标"""
        status = "success" if success else "failed"
        self._tool_metrics.append({
            "toolName": tool_name,
            "layer": layer.value,
            "success": success,
            "durationMs": round(duration_ms, 3),
            "cacheHit": cache_hit,
            "retryCount": retry_count,
            "recordsProcessed": records_processed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # 保持最近 1000 条记录
        if len(self._tool_metrics) > 1000:
            self._tool_metrics = self._tool_metrics[-1000:]

    def get_summary(self, window_minutes: int = 60) -> dict[str, Any]:
        """获取指标摘要"""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        recent = [
            m for m in self._tool_metrics
            if datetime.fromisoformat(m["timestamp"]) > cutoff
        ]

        total_calls = len(recent)
        success_calls = sum(1 for m in recent if m["success"])
        failed_calls = sum(1 for m in recent if not m["success"])
        cache_hits = sum(1 for m in recent if m["cacheHit"])
        total_duration = sum(m["durationMs"] for m in recent)
        total_records = sum(m.get("recordsProcessed", 0) for m in recent)

        # 按工具聚合
        by_tool: dict[str, dict[str, Any]] = {}
        for m in recent:
            name = m["toolName"]
            if name not in by_tool:
                by_tool[name] = {
                    "toolName": name,
                    "layer": m["layer"],
                    "totalCalls": 0,
                    "successCalls": 0,
                    "failedCalls": 0,
                    "avgDurationMs": 0,
                    "cacheHitRate": 0,
                }
            stats = by_tool[name]
            stats["totalCalls"] += 1
            if m["success"]:
                stats["successCalls"] += 1
            else:
                stats["failedCalls"] += 1

        for stats in by_tool.values():
            if stats["totalCalls"] > 0:
                stats["cacheHitRate"] = round(
                    sum(1 for m in recent if m["toolName"] == stats["toolName"] and m["cacheHit"])
                    / stats["totalCalls"] * 100, 1
                )

        # 按层聚合
        by_layer: dict[str, dict[str, Any]] = {}
        for m in recent:
            layer = m["layer"]
            if layer not in by_layer:
                by_layer[layer] = {"layer": layer, "totalCalls": 0, "successRate": 0}
            by_layer[layer]["totalCalls"] += 1
        for layer_stats in by_layer.values():
            total = layer_stats["totalCalls"]
            layer_success = sum(
                1 for m in recent if m["layer"] == layer_stats["layer"] and m["success"]
            )
            layer_stats["successRate"] = round(layer_success / total * 100, 1) if total > 0 else 0

        return {
            "windowMinutes": window_minutes,
            "totalCalls": total_calls,
            "successRate": round(success_calls / total_calls * 100, 1) if total_calls > 0 else 0,
            "failedRate": round(failed_calls / total_calls * 100, 1) if total_calls > 0 else 0,
            "cacheHitRate": round(cache_hits / total_calls * 100, 1) if total_calls > 0 else 0,
            "avgDurationMs": round(total_duration / total_calls, 2) if total_calls > 0 else 0,
            "totalRecordsProcessed": total_records,
            "byTool": list(by_tool.values()),
            "byLayer": list(by_layer.values()),
        }

    def get_counter(self, metric: str, tags: dict[str, str] | None = None) -> int:
        tag_str = _build_tag_suffix(tags)
        return self._counters.get(f"{metric}{tag_str}", 0)

    def get_histogram_stats(
        self,
        metric: str,
        tags: dict[str, str] | None = None,
    ) -> dict[str, float]:
        tag_str = _build_tag_suffix(tags)
        values = self._histograms.get(f"{metric}{tag_str}", [])
        if not values:
            return {"count": 0, "avg": 0, "p50": 0, "p90": 0, "p99": 0, "max": 0}
        sorted_values = sorted(values)
        return {
            "count": len(values),
            "avg": round(sum(values) / len(values), 3),
            "p50": sorted_values[len(values) // 2],
            "p90": sorted_values[int(len(values) * 0.9)],
            "p99": sorted_values[int(len(values) * 0.99)],
            "max": sorted_values[-1],
        }


def _build_tag_suffix(tags: dict[str, str] | None) -> str:
    if not tags:
        return ""
    return ":" + ":".join(f"{k}={v}" for k, v in sorted(tags.items()))


# 全局指标收集器
_task_management_metrics: TaskManagementMetricsSink | None = None


def get_task_management_metrics() -> TaskManagementMetricsSink:
    global _task_management_metrics
    if _task_management_metrics is None:
        _task_management_metrics = TaskManagementMetricsSink()
    return _task_management_metrics
