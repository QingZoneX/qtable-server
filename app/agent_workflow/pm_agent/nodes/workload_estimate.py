from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, WorkloadEstimateItem,
)
from app.skills.contracts import SkillCallRequest, SkillContext
from app.skills.runtime import SkillExecutionContext, SkillRuntime
from app.services.skill_registry import skill_registry_service

logger = logging.getLogger(__name__)


class WorkloadEstimateCallNode:
    """Calls qtable.project.estimate_workload skill to estimate effort."""

    async def estimate(
        self,
        state: PMAgentState,
        db: AsyncSession,
        user_id: int,
        workspace_id: str | None,
    ) -> PMPhaseResult:
        logger.info("WorkloadEstimate(skill) start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            registry = await skill_registry_service.build_runtime_registry(db, workspace_id=workspace_id)
            runtime = SkillRuntime(registry)

            prompt = state.user_message
            if state.task_tree:
                prompt += "\n\nTasks: " + "\n".join(
                    f"- [{t.depth}] {t.title} ({t.estimateHours}h)" for t in state.task_tree[:20]
                )
            if state.modules:
                prompt += "\n\nModules: " + "\n".join(
                    f"- {m.name}: {m.description}" for m in state.modules
                )

            execution_context = SkillExecutionContext(
                context=SkillContext(
                    user_id=user_id, workspace_id=workspace_id,
                    session_id=state.session_id, conversation_id=state.conversation_id,
                    project_id=state.project_id, table_ids=state.table_ids,
                    view_id=state.view_id, task_id=state.task_id,
                    team_id=state.team_id, organization_id=state.organization_id,
                    workflow_id=state.workflow_id, agent_id=state.agent_id,
                    trace_id=state.workflow_id, origin="workflow",
                    locale=state.locale, timezone=state.timezone,
                    dry_run=False, confirmed=True,
                ),
                db=db,
            )

            response = await runtime.invoke(
                SkillCallRequest(
                    call_id=state.workflow_id,
                    skill_name="qtable.project.estimate_workload",
                    input={
                        "prompt": prompt,
                        "persistResult": False,
                        "dryRun": False,
                        "workspaceId": workspace_id,
                        "locale": state.locale,
                        "timezone": state.timezone,
                    },
                    dry_run=False, confirmed=True,
                    origin="workflow", trace_id=state.workflow_id,
                ),
                execution_context,
            )

            if response.error:
                return PMPhaseResult(
                    phase=PMPhase.WORKLOAD_ESTIMATE, status=PMPhaseStatus.FAILED,
                    startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                    error={"code": response.error.code.value, "message": response.error.message},
                )

            result_data = response.output or {}
            result = result_data.get("result", result_data)

            estimates: list[WorkloadEstimateItem] = []
            breakdown = result.get("breakdown", [])
            for item in breakdown:
                estimates.append(WorkloadEstimateItem(
                    moduleName=item.get("name", ""),
                    storyPoints=item.get("storyPoints", item.get("story_points", 0)),
                    p50Hours=item.get("p50Hours", item.get("p50_hours", 0)),
                    p90Hours=item.get("p90Hours", item.get("p90_hours", 0)),
                    riskCoefficient=item.get("riskCoefficient", item.get("risk_coefficient", 1.0)),
                    assignedMembers=item.get("assignedMembers", 1),
                ))

            return PMPhaseResult(
                phase=PMPhase.WORKLOAD_ESTIMATE, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={
                    "estimates": [e.model_dump(mode="json", by_alias=True) for e in estimates],
                    "summary": result.get("summary", ""),
                    "totalStoryPoints": result.get("adjustedStoryPoints", result.get("story_points", 0)),
                    "totalP50Hours": result.get("p50Hours", 0),
                    "totalP90Hours": result.get("p90Hours", 0),
                    "recommendedTeamSize": 4,
                    "confidenceLevel": result.get("confidence", {}).get("level", "medium") if isinstance(result.get("confidence"), dict) else "medium",
                    "assumptions": result.get("assumptions", []),
                    "raw": result,
                },
            )
        except Exception as exc:
            logger.exception("WorkloadEstimate(skill) failed workflow_id=%s", state.workflow_id)
            return PMPhaseResult(
                phase=PMPhase.WORKLOAD_ESTIMATE, status=PMPhaseStatus.FAILED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                error={"code": "WORKLOAD_ESTIMATE_SKILL_FAILED", "message": str(exc)},
            )


workload_estimate_node = WorkloadEstimateCallNode()
