from __future__ import annotations

import logging

from app.agent_runtime.types import (
    ObservationRecord,
    ToolCallRecord,
    ToolCallState,
    ToolPlanStep,
    ToolPlanStepStatus,
)

logger = logging.getLogger(__name__)


class Observer:
    def observe_tool_call(
        self,
        tool_call: ToolCallRecord,
        step: ToolPlanStep | None = None,
    ) -> ObservationRecord:
        summary_parts: list[str] = []
        insights: list[str] = []
        suggestions: list[str] = []

        if tool_call.state == ToolCallState.COMPLETED:
            summary_parts.append(f"工具 {tool_call.skill_name} 执行成功")
            if tool_call.output:
                output_keys = list(tool_call.output.keys())[:5]
                summary_parts.append(f"返回字段: {output_keys}")

            if tool_call.duration_ms > 3000:
                insights.append(
                    f"工具 {tool_call.skill_name} 执行耗时 {tool_call.duration_ms:.0f}ms，可能需优化"
                )

        elif tool_call.state == ToolCallState.FAILED:
            summary_parts.append(f"工具 {tool_call.skill_name} 执行失败")
            if tool_call.error:
                summary_parts.append(f"错误: {tool_call.error.get('code', 'unknown')}")

            if tool_call.attempt_count > 1:
                suggestions.append(f"建议调整 {tool_call.skill_name} 的参数后重试")

            if tool_call.error:
                error_code = tool_call.error.get("code", "")
                if error_code == "INVALID_INPUT":
                    suggestions.append("输入参数不合法，请检查参数格式和必填项")
                elif error_code == "FORBIDDEN":
                    suggestions.append("权限不足，需要更高权限执行此操作")
                elif error_code == "EXECUTION_FAILED":
                    insights.append("执行错误可能是暂时性的，可尝试重试")

        elif tool_call.state == ToolCallState.REQUIRES_CONFIRMATION:
            summary_parts.append(f"工具 {tool_call.skill_name} 需要用户确认后继续")

        summary = ". ".join(summary_parts) if summary_parts else \
            f"工具 {tool_call.skill_name} -> {tool_call.state.value}"

        return ObservationRecord(
            source_tool_call_id=tool_call.tool_call_id,
            summary=summary,
            insights=insights,
            data={
                "skillName": tool_call.skill_name,
                "state": tool_call.state.value,
                "durationMs": tool_call.duration_ms,
                "attemptCount": tool_call.attempt_count,
                "hasOutput": tool_call.output is not None,
                "hasError": tool_call.error is not None,
            },
            suggestions=suggestions,
        )

    def determine_next_action(
        self,
        observations: list[ObservationRecord],
        pending_steps: list[ToolPlanStep],
        has_pending_approvals: bool,
    ) -> str:
        if not pending_steps:
            return "all_complete"

        if has_pending_approvals:
            return "continue"

        recent = observations[-5:] if len(observations) >= 5 else observations
        failed_count = sum(1 for o in recent if o.data.get("state") == "failed")
        completed_count = sum(1 for o in recent if o.data.get("state") == "completed")

        if failed_count > 0 and completed_count == 0:
            return "replan"

        if failed_count > 0:
            return "continue"

        return "continue"


_observer: Observer | None = None


def get_observer() -> Observer:
    global _observer
    if _observer is None:
        _observer = Observer()
    return _observer
