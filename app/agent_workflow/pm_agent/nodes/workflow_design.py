from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, WorkflowDesign,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 PM Agent 的工作流设计专家。输出 Structured Output（非 Tool Calling）。
为项目设计开发工作流（stages/transitions/roles/automationRules）。"""


class WorkflowDesignOutput(BaseModel):
    stages: list[dict[str, Any]] = Field(default_factory=list)
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    roles: list[dict[str, Any]] = Field(default_factory=list)
    automation_rules: list[dict[str, Any]] = Field(default_factory=list)
    workflow_type: str = "scrum"
    description: str = ""


class WorkflowDesignNode:
    async def design(self, state: PMAgentState, model: OpenAIChatModel) -> PMPhaseResult:
        logger.info("WorkflowDesign start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            agent = Agent(
                model=model, deps_type=dict, output_type=WorkflowDesignOutput,
                system_prompt=SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.15),
            )
            prompt = f"项目需求: {state.user_message}\n"
            if state.member_assignments:
                prompt += f"团队: {[(a.name, a.role) for a in state.member_assignments]}"

            result = await agent.run(prompt, deps={})
            output = result.output
            wf = WorkflowDesign(stages=output.stages, transitions=output.transitions,
                                roles=output.roles, automationRules=output.automation_rules)

            return PMPhaseResult(
                phase=PMPhase.WORKFLOW_DESIGN, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={"workflowDesign": wf.model_dump(mode="json", by_alias=True),
                      "workflowType": output.workflow_type, "description": output.description},
                tokenUsage={"total": result.usage().total_tokens if result.usage() else 0},
            )
        except Exception as exc:
            logger.exception("WorkflowDesign failed")
            return PMPhaseResult(phase=PMPhase.WORKFLOW_DESIGN, status=PMPhaseStatus.FAILED,
                                 startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                                 error={"code": "WORKFLOW_DESIGN_FAILED", "message": str(exc)})


workflow_design_node = WorkflowDesignNode()
