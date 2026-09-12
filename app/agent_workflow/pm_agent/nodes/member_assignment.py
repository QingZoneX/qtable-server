from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, MemberAssignment,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 PM Agent 的团队分配专家。输出 Structured Output（非 Tool Calling）。
基于模块拆分和工时评估，合理分配团队成员。每个成员利用率不超过0.85。"""


class MemberAssignmentOutput(BaseModel):
    assignments: list[dict[str, Any]] = Field(default_factory=list)
    team_summary: str = ""
    unassigned_modules: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class MemberAssignmentNode:
    async def assign(self, state: PMAgentState, model: OpenAIChatModel) -> PMPhaseResult:
        logger.info("MemberAssignment start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            agent = Agent(
                model=model, deps_type=dict, output_type=MemberAssignmentOutput,
                system_prompt=SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.15),
            )
            prompt = f"项目需求: {state.user_message}\n"
            if state.modules:
                prompt += f"模块: {[(m.id, m.name) for m in state.modules]}"
            if state.workload_estimates:
                prompt += f"\n工时估算: {[(e.moduleName, e.p50Hours) for e in state.workload_estimates]}"

            result = await agent.run(prompt, deps={})
            output = result.output

            assignments = [MemberAssignment(
                userId=a.get("userId", 0), name=a.get("name", ""), role=a.get("role", ""),
                moduleIds=a.get("moduleIds", []), taskIds=a.get("taskIds", []),
                totalHours=a.get("totalHours", 0), utilization=a.get("utilization", 0),
            ) for a in output.assignments]

            return PMPhaseResult(
                phase=PMPhase.MEMBER_ASSIGNMENT, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={"assignments": [a.model_dump(mode="json", by_alias=True) for a in assignments],
                      "teamSummary": output.team_summary, "unassignedModules": output.unassigned_modules,
                      "recommendations": output.recommendations},
                tokenUsage={"total": result.usage().total_tokens if result.usage() else 0},
            )
        except Exception as exc:
            logger.exception("MemberAssignment failed")
            return PMPhaseResult(phase=PMPhase.MEMBER_ASSIGNMENT, status=PMPhaseStatus.FAILED,
                                 startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                                 error={"code": "MEMBER_ASSIGNMENT_FAILED", "message": str(exc)})


member_assignment_node = MemberAssignmentNode()
