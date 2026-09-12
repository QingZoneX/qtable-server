from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.agent_workflow.state import (
    WorkflowAgentState,
    ObservationRecord,
    ToolCallRecord,
    WorkflowStepStatus,
)

logger = logging.getLogger(__name__)


class ObservationNode:
    async def observe(self, state: WorkflowAgentState) -> WorkflowAgentState:
        state.status = "observing"

        recent_calls = [tc for tc in state.tool_calls if not getattr(tc, "_observed", False)]
        for call in recent_calls[-3:]:
            observation = self._build_observation(call, state)
            state.observations.append(observation)
            setattr(call, "_observed", True)

        self._aggregate_insights(state)

        logger.info(
            "ObservationNode processed workflow_id=%s new_observations=%d total=%d",
            state.workflow_id,
            len(recent_calls[-3:]),
            len(state.observations),
        )
        return state

    def _build_observation(
        self,
        call: ToolCallRecord,
        state: WorkflowAgentState,
    ) -> ObservationRecord:
        summary_parts = []
        insights = []
        suggestions = []

        if call.state == "completed":
            summary_parts.append(f"工具 {call.skill_name} 执行成功")
            if call.output:
                output_keys = list(call.output.keys())[:5]
                summary_parts.append(f"返回字段: {output_keys}")

            if call.duration_ms > 3000:
                insights.append(f"工具 {call.skill_name} 执行耗时 {call.duration_ms:.0f}ms，可能需优化")

        elif call.state == "failed":
            summary_parts.append(f"工具 {call.skill_name} 执行失败")
            if call.error:
                summary_parts.append(f"错误: {call.error.get('code', 'unknown')}")

            if call.attempt_count > 1:
                suggestions.append(f"建议调整 {call.skill_name} 的参数后重试")

            if call.error:
                error_code = call.error.get("code", "")
                if error_code == "INVALID_INPUT":
                    suggestions.append("输入参数不合法，请检查参数格式和必填项")
                elif error_code == "FORBIDDEN":
                    suggestions.append("权限不足，需要更高权限执行此操作")
                elif error_code == "EXECUTION_FAILED":
                    insights.append("执行错误可能是暂时性的，可尝试重试")

        elif call.state == "requires_confirmation":
            summary_parts.append(f"工具 {call.skill_name} 需要用户确认后继续")

        return ObservationRecord(
            sourceCallId=call.call_id,
            summary=". ".join(summary_parts) if summary_parts else f"工具 {call.skill_name} -> {call.state}",
            insights=insights,
            data={
                "skillName": call.skill_name,
                "state": call.state,
                "durationMs": call.duration_ms,
                "attemptCount": call.attempt_count,
                "hasOutput": call.output is not None,
                "hasError": call.error is not None,
            },
            suggestions=suggestions,
        )

    def _aggregate_insights(self, state: WorkflowAgentState) -> None:
        if not state.observations:
            return

        recent = state.observations[-5:]
        failed_count = sum(1 for o in recent if o.data.get("state") == "failed")
        completed_count = sum(1 for o in recent if o.data.get("state") == "completed")
        pending_approval_count = sum(
            1 for o in recent if o.data.get("state") == "requires_confirmation"
        )

        if failed_count > 0 and completed_count == 0:
            state.observations[-1].suggestions.append(
                "连续执行失败，建议检查 Skill 配置或调整策略"
            )

        if pending_approval_count > 0 and state.status != "awaiting_approval":
            state.status = "awaiting_approval"
            for step in (state.plan.steps if state.plan else []):
                if step.status == WorkflowStepStatus.REQUIRES_APPROVAL:
                    break


observation_node = ObservationNode()
