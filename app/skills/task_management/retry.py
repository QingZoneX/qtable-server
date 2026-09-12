"""
Task Management - Tool Retry

指数退避重试策略，与现有 ToolRetryConfig 集成。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.tool_adapter.contracts import ToolRetryConfig

logger = logging.getLogger(__name__)


class TaskManagementRetryConfig(BaseModel):
    """任务管理工具专用重试配置"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # 基础重试
    max_attempts: int = Field(default=2, ge=1, le=5, alias="maxAttempts")
    base_backoff_ms: int = Field(default=350, ge=100, le=10000, alias="baseBackoffMs")
    max_backoff_ms: int = Field(default=30000, ge=1000, le=60000, alias="maxBackoffMs")
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=5.0, alias="backoffMultiplier")

    # 超时
    request_timeout_s: int = Field(default=30, ge=5, le=300, alias="requestTimeoutS")

    # 可重试错误类型
    retryable_error_codes: list[str] = Field(
        default_factory=lambda: [
            "EXECUTION_FAILED",
            "TIMEOUT",
            "TEMPORARY_ERROR",
            "RATE_LIMITED",
        ],
        alias="retryableErrorCodes",
    )

    # 不可重试错误类型
    non_retryable_error_codes: list[str] = Field(
        default_factory=lambda: [
            "UNAUTHORIZED",
            "FORBIDDEN",
            "INVALID_INPUT",
            "SKILL_NOT_FOUND",
        ],
        alias="nonRetryableErrorCodes",
    )

    def to_tool_retry_config(self) -> ToolRetryConfig:
        return ToolRetryConfig(
            maxAttempts=self.max_attempts,
            backoffMs=self.base_backoff_ms,
        )


async def with_task_management_retry(
    func,
    *args,
    retry_config: TaskManagementRetryConfig | None = None,
    tool_name: str = "unknown",
    **kwargs,
) -> tuple[dict[str, Any], int, float]:
    """
    带重试的 Task Management 工具调用包装器。

    返回: (result, attempt_count, duration_ms)
    """
    config = retry_config or TaskManagementRetryConfig()
    start = datetime.now(timezone.utc)

    last_error: Exception | None = None
    attempt = 0

    for attempt in range(1, config.max_attempts + 1):
        try:
            result = await asyncio.wait_for(
                func(*args, **kwargs),
                timeout=config.request_timeout_s,
            )
            duration_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
            if attempt > 1:
                logger.info(
                    "Task management tool %s succeeded on attempt %d",
                    tool_name,
                    attempt,
                )
            return result, attempt, duration_ms
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"Tool {tool_name} timed out after {config.request_timeout_s}s")
            logger.warning("Tool %s attempt %d/%d timed out", tool_name, attempt, config.max_attempts)
        except Exception as exc:
            last_error = exc
            error_str = str(exc).lower()

            # 检查是否可重试
            is_retryable = False
            for code in config.retryable_error_codes:
                if code.lower() in error_str:
                    is_retryable = True
                    break

            for code in config.non_retryable_error_codes:
                if code.lower() in error_str:
                    is_retryable = False
                    break

            if not is_retryable and attempt < config.max_attempts:
                logger.warning(
                    "Tool %s failed with non-retryable error, stopping: %s",
                    tool_name,
                    str(exc)[:200],
                )
                break

            logger.warning(
                "Tool %s attempt %d/%d failed: %s (retryable=%s)",
                tool_name,
                attempt,
                config.max_attempts,
                str(exc)[:200],
                is_retryable,
            )

        if attempt < config.max_attempts:
            backoff = min(
                config.base_backoff_ms * (config.backoff_multiplier ** (attempt - 1)),
                config.max_backoff_ms,
            )
            backoff += (hash(tool_name + str(attempt)) % 200)  # jitter
            await asyncio.sleep(backoff / 1000)

    duration_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000

    error_msg = str(last_error) if last_error else "Unknown error"
    return {
        "success": False,
        "error": {
            "code": "RETRY_EXHAUSTED",
            "message": f"All {attempt} retry attempts exhausted: {error_msg}",
            "attempts": attempt,
        },
    }, attempt, duration_ms


# 预设重试配置
RETRY_CONFIG_READ_ONLY = TaskManagementRetryConfig(
    max_attempts=2,
    base_backoff_ms=350,
    request_timeout_s=15,
)

RETRY_CONFIG_ANALYTICS = TaskManagementRetryConfig(
    max_attempts=2,
    base_backoff_ms=500,
    request_timeout_s=30,
)

RETRY_CONFIG_WORKFLOW = TaskManagementRetryConfig(
    max_attempts=3,
    base_backoff_ms=500,
    request_timeout_s=60,
)
