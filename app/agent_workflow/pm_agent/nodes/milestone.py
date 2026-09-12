from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, MilestoneItem,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 PM Agent 的里程碑规划专家。输出 Structured Output（非 Tool Calling）。
基于项目范围和工时评估设定关键里程碑。"""


class MilestoneOutput(BaseModel):
    milestones: list[dict[str, Any]] = Field(default_factory=list)
    timeline_summary: str = ""
    project_start: str = ""
    project_end: str = ""
    total_duration_weeks: int = 0


class MilestoneNode:
    async def plan(self, state: PMAgentState, model: OpenAIChatModel) -> PMPhaseResult:
        logger.info("Milestone start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            agent = Agent(
                model=model, deps_type=dict, output_type=MilestoneOutput,
                system_prompt=SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.15),
            )
            prompt = f"项目需求: {state.user_message}\n"
            if state.workload_estimates:
                prompt += f"工时估算: {[(e.moduleName, e.p50Hours) for e in state.workload_estimates]}"

            result = await agent.run(prompt, deps={})
            output = result.output

            milestones = [MilestoneItem(
                title=m.get("title", ""), description=m.get("description", ""),
                dueDate=m.get("dueDate"), moduleIds=m.get("moduleIds", []),
                deliverables=m.get("deliverables", []), status=m.get("status", "planned"),
            ) for m in output.milestones]

            return PMPhaseResult(
                phase=PMPhase.MILESTONE, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={"milestones": [m.model_dump(mode="json", by_alias=True) for m in milestones],
                      "timelineSummary": output.timeline_summary, "projectStart": output.project_start,
                      "projectEnd": output.project_end, "totalDurationWeeks": output.total_duration_weeks},
                tokenUsage={"total": result.usage().total_tokens if result.usage() else 0},
            )
        except Exception as exc:
            logger.exception("Milestone failed")
            return PMPhaseResult(phase=PMPhase.MILESTONE, status=PMPhaseStatus.FAILED,
                                 startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                                 error={"code": "MILESTONE_FAILED", "message": str(exc)})


milestone_node = MilestoneNode()
