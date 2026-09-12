"""
Task Management - Tool Streaming

流式输出支持，与现有 SSE Bridge 集成。
提供渐进式数据推送能力，用于长时间运行的分析任务。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncGenerator, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class StreamEventType(str, Enum):
    """流事件类型"""
    STARTED = "started"
    PROGRESS = "progress"
    PARTIAL_RESULT = "partial_result"
    INSIGHT = "insight"
    WARNING = "warning"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class StreamEvent(BaseModel):
    """流事件"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: StreamEventType
    tool_name: str = Field(alias="toolName")
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    message: str = ""
    data: Optional[dict[str, Any]] = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_sse(self) -> str:
        """转换为 SSE 格式"""
        payload = self.model_dump(mode="json", by_alias=True)
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class StreamingAnalysisResult(BaseModel):
    """流式分析结果"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tool_name: str = Field(alias="toolName")
    total_items: int = Field(default=0, alias="totalItems")
    processed_items: int = Field(default=0, alias="processedItems")
    data: list[dict[str, Any]] = Field(default_factory=list)
    insights: list[str] = Field(default_factory=list)
    duration_ms: float = Field(default=0.0, alias="durationMs")


async def stream_analysis(
    tool_name: str,
    data_source: list[dict[str, Any]],
    *,
    batch_size: int = 10,
    progress_callback=None,
    cancel_event: asyncio.Event | None = None,
) -> AsyncGenerator[StreamEvent, None]:
    """
    流式分析生成器。

    渐进式推送分析进度和部分结果，支持取消。

    用法:
        async for event in stream_analysis(
            tool_name="get_overdue_tasks",
            data_source=all_records,
            batch_size=20,
        ):
            yield event.to_sse()
    """
    start_time = datetime.now(timezone.utc)
    total = len(data_source)
    processed = 0

    yield StreamEvent(
        type=StreamEventType.STARTED,
        toolName=tool_name,
        progress=0.0,
        message=f"开始分析，共 {total} 项数据...",
    )

    for i in range(0, total, batch_size):
        if cancel_event and cancel_event.is_set():
            yield StreamEvent(
                type=StreamEventType.CANCELLED,
                toolName=tool_name,
                progress=processed / total if total > 0 else 0,
                message="分析已取消",
            )
            return

        batch = data_source[i:i + batch_size]
        processed += len(batch)
        progress = processed / total if total > 0 else 1.0

        # 模拟批次处理
        await asyncio.sleep(0.01)

        yield StreamEvent(
            type=StreamEventType.PROGRESS,
            toolName=tool_name,
            progress=progress,
            message=f"处理中... {processed}/{total}",
        )

        # 每处理 25% 推送一次部分结果
        if processed % max(1, total // 4) == 0 or processed == total:
            yield StreamEvent(
                type=StreamEventType.PARTIAL_RESULT,
                toolName=tool_name,
                progress=progress,
                data={
                    "processedItems": processed,
                    "totalItems": total,
                    "partialData": batch[:3],
                },
                message=f"已完成 {processed}/{total} 项",
            )

        if progress_callback:
            await progress_callback(progress, processed, total)

    duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

    yield StreamEvent(
        type=StreamEventType.COMPLETED,
        toolName=tool_name,
        progress=1.0,
        data={
            "totalItems": total,
            "processedItems": processed,
            "durationMs": round(duration_ms, 2),
        },
        message=f"分析完成，耗时 {duration_ms:.0f}ms",
    )


async def stream_with_progress(
    tool_name: str,
    fetcher,
    *,
    cancel_event: asyncio.Event | None = None,
) -> AsyncGenerator[StreamEvent, None]:
    """
    通用流式包装器。

    适用于任何返回 list[dict] 的 Task Management 工具。
    """
    yield StreamEvent(
        type=StreamEventType.STARTED,
        toolName=tool_name,
        progress=0.0,
        message=f"开始执行 {tool_name}...",
    )

    try:
        yield StreamEvent(
            type=StreamEventType.PROGRESS,
            toolName=tool_name,
            progress=0.3,
            message="查询数据中...",
        )

        if cancel_event and cancel_event.is_set():
            yield StreamEvent(type=StreamEventType.CANCELLED, toolName=tool_name, progress=0.0, message="已取消")
            return

        result = await fetcher()

        yield StreamEvent(
            type=StreamEventType.PROGRESS,
            toolName=tool_name,
            progress=0.7,
            message="分析结果中...",
        )

        yield StreamEvent(
            type=StreamEventType.COMPLETED,
            toolName=tool_name,
            progress=1.0,
            data=result if isinstance(result, dict) else {"result": str(result)[:200]},
            message="执行完成",
        )
    except Exception as exc:
        yield StreamEvent(
            type=StreamEventType.ERROR,
            toolName=tool_name,
            progress=0.0,
            data={"error": str(exc)},
            message=f"执行失败: {str(exc)[:200]}",
        )


# 流式工具注册表
STREAMABLE_TOOLS = {
    "qtable.task.overdue.get",
    "qtable.project.progress.calculate",
    "qtable.member.workload.get",
    "qtable.task.blocking.detect",
    "qtable.project.delay.predict",
    "qtable.workflow.execution_plan.create",
}


def is_streamable(tool_name: str) -> bool:
    """判断工具是否支持流式输出"""
    return tool_name in STREAMABLE_TOOLS
