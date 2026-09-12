from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_runtime.types import (
    AgentIntent,
    IntentResult,
    ToolPlan,
    ToolPlanStep,
    ToolPlanStepStatus,
)

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """你是 QTable AI Runtime 的执行计划编排器 (Planner)。

你的职责：
1. 根据用户意图和上下文，制定结构化执行计划 (ToolPlan)。
2. 将复杂任务分解为有序步骤 (ToolPlanStep)。
3. 每个步骤明确指定 Skill 名称、参数和依赖关系。
4. 识别写入类操作，标记 require_approval=true。
5. 考虑数据依赖，确保执行顺序合理。

规划原则：
- 读取类 Skill 优先，写入类 Skill 后置。
- 有依赖的步骤按顺序排列，无依赖的步骤标记 parallel_group 以支持并行。
- 每步需有清晰描述和预期输出。
- chat 意图无需步骤，analyze 意图只需要读取类步骤。
- action/multi_step 意图需完整编排。

输出格式：严格 JSON。
"""


class PlannerOutput(BaseModel):
    reasoning: str = ""
    goal: str = ""
    strategy: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    requires_confirmation: bool = Field(default=False)


class Planner:
    def __init__(self) -> None:
        self._agent: PydanticAgent[dict[str, Any], PlannerOutput] | None = None

    async def plan(
        self,
        message: str,
        intent: IntentResult,
        model: OpenAIChatModel,
        context: dict[str, Any],
    ) -> ToolPlan:
        agent = PydanticAgent(
            model=model,
            deps_type=dict,
            output_type=PlannerOutput,
            system_prompt=PLANNER_SYSTEM_PROMPT,
            model_settings=OpenAIChatModelSettings(temperature=0.15),
        )
        prompt = self._build_planning_prompt(message, intent, context)
        result = await agent.run(prompt, deps={})
        output = result.output

        steps: list[ToolPlanStep] = []
        for index, step_data in enumerate(output.steps):
            step = ToolPlanStep(
                step_id=step_data.get("step_id", str(uuid.uuid4())),
                index=step_data.get("index", index),
                description=step_data.get("description", ""),
                skill_name=step_data.get("skill_name"),
                function_name=step_data.get("function_name"),
                arguments=step_data.get("arguments", {}),
                status=ToolPlanStepStatus.PENDING,
                depends_on=step_data.get("depends_on", []),
                parallel_group=step_data.get("parallel_group"),
                require_approval=step_data.get("require_approval", False),
                max_retries=step_data.get("max_retries", 3),
                timeout_seconds=step_data.get("timeout_seconds", 60),
            )
            steps.append(step)

        plan = ToolPlan(
            goal=output.goal or message,
            strategy=output.strategy,
            reasoning=output.reasoning,
            intent=intent.intent.value,
            intent_confidence=intent.confidence,
            steps=steps,
            estimated_steps=len(steps),
            requires_confirmation=output.requires_confirmation or intent.requires_confirmation,
            planner_metadata={
                "raw_steps": output.steps,
            },
            langgraph_spec=self._build_langgraph_spec(steps),
        )

        logger.info(
            "Planner: goal=%s steps=%d intent=%s",
            plan.goal[:100],
            len(steps),
            plan.intent,
        )
        return plan

    def _build_planning_prompt(
        self,
        message: str,
        intent: IntentResult,
        context: dict[str, Any],
    ) -> str:
        parts = [
            f"用户消息: {message}",
            f"识别意图: {intent.intent.value} (置信度: {intent.confidence})",
            f"意图推理: {intent.reasoning}",
            f"建议 Skill: {intent.suggested_skills}",
            f"工作空间: {context.get('workspace_id', 'unknown')}",
            f"数据表: {context.get('table_ids', [])}",
            f"需要确认: {intent.requires_confirmation}",
        ]

        available_skills = context.get("available_skills", [])
        if available_skills:
            parts.append(f"可用 Skills: {available_skills}")

        denied_skills = context.get("denied_skills", [])
        if denied_skills:
            parts.append(f"拒绝 Skills: {denied_skills}")

        if intent.intent == AgentIntent.CHAT:
            parts.append("注意: 这是纯对话意图，无需生成工具调用步骤。")

        return "\n".join(parts)

    def _build_langgraph_spec(self, steps: list[ToolPlanStep]) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []

        for step in steps:
            nodes.append({
                "id": step.step_id,
                "type": "tool",
                "skill_name": step.skill_name,
                "description": step.description,
                "parallel_group": step.parallel_group,
            })
            for dep in step.depends_on:
                edges.append({"from": dep, "to": step.step_id})

        return {
            "nodes": nodes,
            "edges": edges,
            "entry_point": steps[0].step_id if steps else None,
        }


_planner: Planner | None = None


def get_planner() -> Planner:
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner
