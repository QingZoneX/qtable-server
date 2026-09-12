from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.pm_agent.state import PMAgentState, PMPhase

logger = logging.getLogger(__name__)

PM_PLANNER_SYSTEM_PROMPT = """你是 QTable PM Agent 的智能规划器。

你的任务：分析用户的项目需求，从以下10个阶段中选择需要执行的阶段并排序：

1. requirement_analysis - 需求分析：理解项目目标，输出结构化需求
2. module_split - 模块拆分：将系统拆分为功能模块
3. task_tree - 任务树：将模块细化为层级任务（会调用 qtable.task.split Skill）
4. workload_estimate - 工时评估：估算每个模块的工作量（会调用 qtable.project.estimate_workload Skill）
5. member_assignment - 成员分配：建议团队角色和分配
6. milestone - 里程碑：设定关键交付节点
7. gantt - 甘特图：生成项目甘特图（会调用 qtable.gantt.generate Skill）
8. risk_analysis - 风险识别：识别项目风险并制定缓解策略
9. workflow_design - 工作流设计：设计开发流程
10. report - 项目报告：汇总所有结果生成最终报告

原则：
- 需求分析永远是第一步
- 报告永远是最后一步
- 根据项目复杂度，简单项目可跳过 workflow_design
- 始终按以上顺序排列所选阶段"""


class PMPlannerOutput(BaseModel):
    reasoning: str = ""
    goal: str = ""
    strategy: str = ""
    phases: list[str] = Field(default_factory=list)
    skip_reasons: list[str] = Field(default_factory=list)
    estimated_complexity: str = "medium"


class PMPlannerNode:
    def _build_prompt(self, state: PMAgentState) -> str:
        parts = [
            f"项目需求: {state.user_message}",
            f"工作空间: {state.workspace_id or 'unknown'}",
            f"项目ID: {state.project_id or 'unknown'}",
            f"团队ID: {state.team_id or 'unknown'}",
        ]
        if state.messages:
            recent = state.messages[-5:]
            parts.append(f"对话历史: {json.dumps(recent, ensure_ascii=False, default=str)}")
        if state.agent_memory and state.agent_memory.context_summary:
            parts.append(f"记忆摘要: {state.agent_memory.context_summary}")
        return "\n".join(parts)

    async def plan(self, state: PMAgentState, model: OpenAIChatModel) -> list[PMPhase]:
        agent = Agent(
            model=model,
            deps_type=dict,
            output_type=PMPlannerOutput,
            system_prompt=PM_PLANNER_SYSTEM_PROMPT,
            model_settings=OpenAIChatModelSettings(temperature=0.1),
        )
        result = await agent.run(self._build_prompt(state), deps={})
        output = result.output

        phases: list[PMPhase] = []
        for name in output.phases:
            try:
                phases.append(PMPhase(name.strip()))
            except ValueError:
                logger.warning("PM Planner: unknown phase=%s", name)

        if not phases:
            phases = list(PMPhase)

        logger.info("PM Planner: goal=%s phases=%s", output.goal[:100], [p.value for p in phases])
        return phases


pm_planner_node = PMPlannerNode()
