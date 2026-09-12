from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, RiskItem,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 PM Agent 的风险管理专家。输出 Structured Output（非 Tool Calling）。
识别项目风险（技术/人员/进度/质量/外部），给出缓解策略和应急预案。"""


class RiskAnalysisOutput(BaseModel):
    risks: list[dict[str, Any]] = Field(default_factory=list)
    overall_risk_level: str = "medium"
    top_risks: list[str] = Field(default_factory=list)
    risk_matrix: dict[str, Any] = Field(default_factory=dict)


class RiskAnalysisNode:
    async def analyze(self, state: PMAgentState, model: OpenAIChatModel) -> PMPhaseResult:
        logger.info("RiskAnalysis start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            agent = Agent(
                model=model, deps_type=dict, output_type=RiskAnalysisOutput,
                system_prompt=SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.2),
            )
            prompt = f"项目需求: {state.user_message}\n"
            if state.modules:
                prompt += f"模块: {[(m.id, m.name) for m in state.modules]}"

            result = await agent.run(prompt, deps={})
            output = result.output

            risks = [RiskItem(
                title=r.get("title", ""), description=r.get("description", ""),
                category=r.get("category", ""), level=r.get("level", "medium"),
                probability=r.get("probability", 0.0), impact=r.get("impact", 0.0),
                mitigation=r.get("mitigation", ""), contingency=r.get("contingency", ""),
                owner=r.get("owner"), status=r.get("status", "open"),
            ) for r in output.risks]

            return PMPhaseResult(
                phase=PMPhase.RISK_ANALYSIS, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={"risks": [r.model_dump(mode="json", by_alias=True) for r in risks],
                      "overallRiskLevel": output.overall_risk_level,
                      "topRisks": output.top_risks, "riskMatrix": output.risk_matrix},
                tokenUsage={"total": result.usage().total_tokens if result.usage() else 0},
            )
        except Exception as exc:
            logger.exception("RiskAnalysis failed")
            return PMPhaseResult(phase=PMPhase.RISK_ANALYSIS, status=PMPhaseStatus.FAILED,
                                 startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                                 error={"code": "RISK_ANALYSIS_FAILED", "message": str(exc)})


risk_analysis_node = RiskAnalysisNode()
