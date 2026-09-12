from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, RequirementItem,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 PM Agent 的需求分析专家。输出 Structured Output（非 Tool Calling）。
分析项目需求，输出结构化需求列表。每条需求包含 title/description/priority/category/acceptanceCriteria/dependencies。"""


class RequirementAnalysisOutput(BaseModel):
    analysis_summary: str = ""
    scope: str = ""
    out_of_scope: list[str] = Field(default_factory=list)
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    key_stakeholders: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class RequirementAnalysisNode:
    async def analyze(self, state: PMAgentState, model: OpenAIChatModel) -> PMPhaseResult:
        logger.info("RequirementAnalysis start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            agent = Agent(
                model=model, deps_type=dict, output_type=RequirementAnalysisOutput,
                system_prompt=SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.2),
            )
            prompt = f"项目需求: {state.user_message}"
            if state.requirements:
                prompt += f"\n已有需求: {json.dumps([r.model_dump(mode='json', by_alias=True) for r in state.requirements], ensure_ascii=False)}"
            result = await agent.run(prompt, deps={})
            output = result.output

            reqs = [RequirementItem(
                title=r.get("title", ""), description=r.get("description", ""),
                priority=r.get("priority", "medium"), category=r.get("category", ""),
                acceptanceCriteria=r.get("acceptance_criteria", r.get("acceptanceCriteria", [])),
                dependencies=r.get("dependencies", []),
            ) for r in output.requirements]

            return PMPhaseResult(
                phase=PMPhase.REQUIREMENT_ANALYSIS, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={"requirements": [r.model_dump(mode="json", by_alias=True) for r in reqs],
                      "analysisSummary": output.analysis_summary, "scope": output.scope,
                      "outOfScope": output.out_of_scope, "keyStakeholders": output.key_stakeholders,
                      "assumptions": output.assumptions},
                tokenUsage={"total": result.usage().total_tokens if result.usage() else 0},
            )
        except Exception as exc:
            logger.exception("RequirementAnalysis failed")
            return PMPhaseResult(phase=PMPhase.REQUIREMENT_ANALYSIS, status=PMPhaseStatus.FAILED,
                                 startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                                 error={"code": "REQUIREMENT_ANALYSIS_FAILED", "message": str(exc)})


requirement_analysis_node = RequirementAnalysisNode()
