from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.state import (
    WorkflowAgentState,
    WorkflowPlan,
    WorkflowStep,
    WorkflowStepStatus,
)

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """你是 QTable Agent Workflow 的 Planner / Orchestrator。

你的职责：
1. 分析用户意图和上下文，制定最优执行计划。
2. 将复杂任务分解为一系列有序的步骤（WorkflowStep）。
3. 每个步骤明确指定需要调用的 Skill、参数和依赖关系。
4. 识别哪些步骤有副作用（写入/外部调用），标记 require_approval。
5. 考虑步骤间的数据依赖，确保执行顺序合理。
6. 如果历史执行中已有结果，复用而非重新规划。

规划原则：
- 读取类 Skill 优先于写入类 Skill。
- 有依赖关系的步骤按顺序排列，无依赖的步骤尽量并行。
- 每个步骤需有清晰的描述和预期输出。
- 若已有足够上下文可直接回答，可返回空步骤列表。
- 如果检测到需要人工确认的操作，在对应步骤标记 require_approval=true。
- 考虑 retry 策略，对可能失败的步骤设置合理的 max_retries。

输出格式：严格 JSON，包含 reasoning 和完整的步骤计划。
"""


class PlannerOutput(BaseModel):
    reasoning: str = ""
    goal: str = ""
    strategy: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)


class PlannerNode:
    def __init__(self) -> None:
        self._agent: Optional[Agent[dict[str, Any], PlannerOutput]] = None

    def _build_planning_prompt(self, state: WorkflowAgentState) -> str:
        context_parts = [
            f"用户消息: {state.user_message}",
            f"工作空间: {state.workspace_id or 'unknown'}",
            f"数据表: {state.table_ids}",
            f"会话ID: {state.session_id or 'unknown'}",
            f"当前状态: {state.status}",
        ]

        if state.observations:
            obs_text = "\n".join(
                f"- [{o.source_call_id}] {o.summary}" for o in state.observations[-5:]
            )
            context_parts.append(f"已有观察结果:\n{obs_text}")

        if state.tool_calls:
            calls_text = "\n".join(
                f"- {tc.skill_name}({json.dumps(tc.arguments, ensure_ascii=False)}) -> {tc.state}"
                for tc in state.tool_calls[-5:]
            )
            context_parts.append(f"已有工具调用:\n{calls_text}")

        if state.pending_approvals:
            approvals_text = "\n".join(
                f"- [{a.approval_id}] {a.skill_name}: {a.reason}"
                for a in state.pending_approvals
                if a.status == "pending"
            )
            context_parts.append(f"待审批操作:\n{approvals_text}")

        available_skills = state.allowed_skills or []
        if state.context_snapshot:
            ctx = state.context_snapshot
            context_parts.append(f"上下文摘要: {json.dumps(ctx, ensure_ascii=False, default=str)}")

        context_parts.append(f"可用 Skills: {available_skills if available_skills else '全部可用'}")
        context_parts.append(f"拒绝 Skills: {state.denied_skills if state.denied_skills else '无'}")

        return "\n".join(context_parts)

    async def plan(self, state: WorkflowAgentState, model: OpenAIChatModel) -> WorkflowPlan:
        agent = Agent(
            model=model,
            deps_type=dict,
            output_type=PlannerOutput,
            system_prompt=PLANNER_SYSTEM_PROMPT,
            model_settings=OpenAIChatModelSettings(temperature=0.15),
        )

        prompt = self._build_planning_prompt(state)
        result = await agent.run(prompt, deps={})
        output = result.output

        steps = []
        for index, step_data in enumerate(output.steps):
            step = WorkflowStep(
                step_id=step_data.get("step_id", str(uuid.uuid4())),
                index=step_data.get("index", index),
                description=step_data.get("description", ""),
                skill_name=step_data.get("skill_name"),
                arguments=step_data.get("arguments", {}),
                status=WorkflowStepStatus.PENDING,
                depends_on=step_data.get("depends_on", []),
                max_retries=step_data.get("max_retries", state.config.max_retries_per_step),
                retry_count=0,
                retry_delay_ms=step_data.get("retry_delay_ms", 1000),
                require_approval=step_data.get("require_approval", False),
            )
            steps.append(step)

        plan = WorkflowPlan(
            goal=output.goal or state.user_message,
            strategy=output.strategy,
            steps=steps,
            reasoning=output.reasoning,
        )

        logger.info(
            "PlannerNode created plan workflow_id=%s steps=%d goal=%s",
            state.workflow_id,
            len(steps),
            plan.goal[:100],
        )
        return plan


planner_node = PlannerNode()
