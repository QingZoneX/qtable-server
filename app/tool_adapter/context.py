from __future__ import annotations

from typing import Any

from app.skills.runtime import SkillExecutionContext
from app.tool_adapter.contracts import ToolAdapterContext


def build_tool_adapter_context(
    *,
    execution_context: SkillExecutionContext,
    skill_name: str,
    function_name: str,
    runtime_context: dict[str, Any] | None = None,
    request_context: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    agent_context: dict[str, Any] | None = None,
) -> ToolAdapterContext:
    return ToolAdapterContext(
        skill={
            "skillName": skill_name,
            "functionName": function_name,
            "skillContext": execution_context.context.model_dump(mode="json"),
        },
        runtime={
            **(runtime_context or {}),
            "agentContext": agent_context or {},
        },
        request=request_context or {},
        state=state or {},
        metrics={},
    )
