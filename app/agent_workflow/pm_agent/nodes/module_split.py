from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, ModuleItem,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 PM Agent 的系统架构专家。输出 Structured Output（非 Tool Calling）。
将系统拆分为功能模块，遵循高内聚低耦合原则。"""


class ModuleSplitOutput(BaseModel):
    architecture_overview: str = ""
    modules: list[dict[str, Any]] = Field(default_factory=list)
    module_dependencies: list[dict[str, Any]] = Field(default_factory=list)
    architecture_style: str = "modular_monolith"
    integration_points: list[str] = Field(default_factory=list)


class ModuleSplitNode:
    async def split(self, state: PMAgentState, model: OpenAIChatModel) -> PMPhaseResult:
        logger.info("ModuleSplit start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            agent = Agent(
                model=model, deps_type=dict, output_type=ModuleSplitOutput,
                system_prompt=SYSTEM_PROMPT,
                model_settings=OpenAIChatModelSettings(temperature=0.2),
            )
            prompt = f"项目需求: {state.user_message}\n"
            if state.requirements:
                prompt += f"需求列表:\n{json.dumps([r.model_dump(mode='json', by_alias=True) for r in state.requirements], ensure_ascii=False)}"

            result = await agent.run(prompt, deps={})
            output = result.output

            modules = [ModuleItem(
                name=m.get("name", ""), description=m.get("description", ""),
                parentModuleId=m.get("parentModuleId"), order=m.get("order", idx),
                responsibilities=m.get("responsibilities", []),
                subModules=m.get("subModules", []),
            ) for idx, m in enumerate(output.modules)]

            return PMPhaseResult(
                phase=PMPhase.MODULE_SPLIT, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={"modules": [m.model_dump(mode="json", by_alias=True) for m in modules],
                      "architectureOverview": output.architecture_overview,
                      "moduleDependencies": output.module_dependencies,
                      "architectureStyle": output.architecture_style,
                      "integrationPoints": output.integration_points},
                tokenUsage={"total": result.usage().total_tokens if result.usage() else 0},
            )
        except Exception as exc:
            logger.exception("ModuleSplit failed")
            return PMPhaseResult(phase=PMPhase.MODULE_SPLIT, status=PMPhaseStatus.FAILED,
                                 startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                                 error={"code": "MODULE_SPLIT_FAILED", "message": str(exc)})


module_split_node = ModuleSplitNode()
