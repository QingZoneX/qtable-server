from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, ProjectReport,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 PM Agent 的报告生成专家。输出 Structured Output（非 Tool Calling）。
汇总所有阶段结果，生成完整项目规划报告。"""


class ReportOutput(BaseModel):
    executive_summary: str = ""
    scope: str = ""
    modules: list[dict[str, Any]] = Field(default_factory=list)
    timeline: str = ""
    total_estimated_hours: float = 0.0
    team_size: int = 0
    risks_summary: list[str] = Field(default_factory=list)
    milestones: list[dict[str, Any]] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class ReportNode:
    async def generate(self, state: PMAgentState, model: OpenAIChatModel) -> PMPhaseResult:
        logger.info("Report start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            all_phase_data: dict[str, Any] = {}
            for pr in state.phase_results:
                if pr.data and isinstance(pr.data, dict):
                    all_phase_data[pr.phase.value] = pr.data

            agent = Agent(
                model=model, deps_type=dict, output_type=ReportOutput,
                system_prompt=SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.2),
            )
            prompt = f"项目需求: {state.user_message}\n\n所有阶段结果:\n{all_phase_data}"
            result = await agent.run(prompt, deps={})
            output = result.output

            report = ProjectReport(
                executiveSummary=output.executive_summary, scope=output.scope,
                modules=output.modules, timeline=output.timeline,
                totalEstimatedHours=output.total_estimated_hours, teamSize=output.team_size,
                risksSummary=output.risks_summary, milestones=output.milestones,
                nextSteps=output.next_steps,
            )

            return PMPhaseResult(
                phase=PMPhase.REPORT, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={"report": report.model_dump(mode="json", by_alias=True)},
                tokenUsage={"total": result.usage().total_tokens if result.usage() else 0},
            )
        except Exception as exc:
            logger.exception("Report failed")
            return PMPhaseResult(phase=PMPhase.REPORT, status=PMPhaseStatus.FAILED,
                                 startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                                 error={"code": "REPORT_FAILED", "message": str(exc)})


report_node = ReportNode()
