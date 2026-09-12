"""
Task Management - Tool Observation

工具可观测性层，提供:
1. 结构化日志
2. 执行指标收集
3. Trace 追踪
4. 健康检查
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.agent_runtime.types import ObservationRecord, ToolCallState
from app.skills.task_management.contracts import (
    TaskManagementToolLayer,
    ToolExecutionMetrics,
)

logger = logging.getLogger(__name__)


class TaskManagementObserver:
    """
    任务管理工具专用观察器。

    为每个工具调用生成：
    - ObservationRecord: 供 Agent Runtime Observer 消费
    - ToolExecutionMetrics: 指标数据
    - 结构化日志
    """

    def observe(
        self,
        tool_name: str,
        layer: TaskManagementToolLayer,
        success: bool,
        duration_ms: float,
        *,
        cache_hit: bool = False,
        retry_count: int = 0,
        db_query_count: int = 0,
        db_query_duration_ms: float = 0.0,
        records_processed: int = 0,
        tool_call_id: str = "",
        error: dict[str, Any] | None = None,
        output_summary: str = "",
    ) -> tuple[ObservationRecord, ToolExecutionMetrics]:
        """观察工具调用，产出 Observation 和 Metrics"""

        # 构建指标
        metrics = ToolExecutionMetrics(
            toolName=tool_name,
            layer=layer,
            durationMs=round(duration_ms, 3),
            cacheHit=cache_hit,
            retryCount=retry_count,
            dbQueryCount=db_query_count,
            dbQueryDurationMs=round(db_query_duration_ms, 3),
            recordsProcessed=records_processed,
            tokenEstimate=_estimate_tokens(output_summary),
        )

        # 构建观察
        state = ToolCallState.COMPLETED if success else ToolCallState.FAILED
        summary_parts: list[str] = []
        insights: list[str] = []
        suggestions: list[str] = []

        if success:
            summary_parts.append(f"[{layer.value}] {tool_name} 执行成功")
            if records_processed > 0:
                summary_parts.append(f"处理 {records_processed} 条记录")
            if cache_hit:
                summary_parts.append("(缓存命中)")
                insights.append(f"{tool_name} 数据来自缓存，时效性可能较低")
            if duration_ms > 5000:
                insights.append(f"{tool_name} 耗时 {duration_ms:.0f}ms，建议优化查询")
        else:
            summary_parts.append(f"[{layer.value}] {tool_name} 执行失败")
            if error:
                summary_parts.append(f"错误: {error.get('code', 'unknown')}")
                if retry_count > 0:
                    suggestions.append(f"已重试 {retry_count} 次，建议检查数据源")

        summary = ". ".join(summary_parts) if summary_parts else f"{tool_name} -> {state.value}"

        observation = ObservationRecord(
            source_tool_call_id=tool_call_id,
            summary=summary,
            insights=insights,
            data={
                "skillName": tool_name,
                "state": state.value,
                "durationMs": duration_ms,
                "cacheHit": cache_hit,
                "retryCount": retry_count,
                "recordsProcessed": records_processed,
            },
            suggestions=suggestions,
        )

        # 结构化日志
        log_data = {
            "tool_name": tool_name,
            "layer": layer.value,
            "success": success,
            "duration_ms": round(duration_ms, 3),
            "cache_hit": cache_hit,
            "retry_count": retry_count,
            "records_processed": records_processed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if success:
            logger.info("TaskManagementTool %s completed", tool_name, extra={"metrics": log_data})
        else:
            logger.error("TaskManagementTool %s failed", tool_name, extra={"metrics": log_data})

        return observation, metrics

    def observe_multi_tool(
        self,
        observations: list[ObservationRecord],
    ) -> dict[str, Any]:
        """观察多工具调用链，产出综合评估"""
        total = len(observations)
        completed = sum(1 for o in observations if o.data.get("state") == "completed")
        failed = sum(1 for o in observations if o.data.get("state") == "failed")
        total_duration = sum(o.data.get("durationMs", 0) for o in observations)
        cache_hits = sum(1 for o in observations if o.data.get("cacheHit"))

        # 质量评估
        if failed > 0:
            chain_health = "degraded" if completed > 0 else "broken"
        elif total_duration > 30000:
            chain_health = "slow"
        elif cache_hits > total / 2:
            chain_health = "stale"
        else:
            chain_health = "healthy"

        return {
            "totalCalls": total,
            "completed": completed,
            "failed": failed,
            "successRate": (completed / total * 100) if total > 0 else 0,
            "totalDurationMs": round(total_duration, 3),
            "cacheHitRate": (cache_hits / total * 100) if total > 0 else 0,
            "chainHealth": chain_health,
        }


def _estimate_tokens(text: str) -> int:
    """估算文本的 token 数量 (粗略: 中文 1.5 字/token, 英文 4 字/token)"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


# 全局观察器实例
_task_management_observer: TaskManagementObserver | None = None


def get_task_management_observer() -> TaskManagementObserver:
    global _task_management_observer
    if _task_management_observer is None:
        _task_management_observer = TaskManagementObserver()
    return _task_management_observer
