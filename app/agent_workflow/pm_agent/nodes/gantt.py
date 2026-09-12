from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, GanttTask,
)
from app.skills.contracts import SkillCallRequest, SkillContext
from app.skills.runtime import SkillExecutionContext, SkillRuntime
from app.services.skill_registry import skill_registry_service

logger = logging.getLogger(__name__)


class GanttCallNode:
    """Calls qtable.gantt.generate skill to produce gantt scheduling data."""

    async def generate(
        self,
        state: PMAgentState,
        db: AsyncSession,
        user_id: int,
        workspace_id: str | None,
    ) -> PMPhaseResult:
        logger.info("Gantt(skill) start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            registry = await skill_registry_service.build_runtime_registry(db, workspace_id=workspace_id)
            runtime = SkillRuntime(registry)

            task_plan: dict[str, Any] = {}
            for pr in state.phase_results:
                if pr.phase == PMPhase.TASK_TREE and pr.data:
                    task_plan = pr.data.get("plan", {})
                    break

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
                    skill_name="qtable.gantt.generate",
                    input={
                        "taskPlan": task_plan,
                        "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        "workingHoursPerDay": 6.0,
                    },
                    dry_run=False, confirmed=True,
                    origin="workflow", trace_id=state.workflow_id,
                ),
                execution_context,
            )

            if response.error:
                return PMPhaseResult(
                    phase=PMPhase.GANTT, status=PMPhaseStatus.FAILED,
                    startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                    error={"code": response.error.code.value, "message": response.error.message},
                )

            result_data = response.output or {}
            items = result_data.get("items", [])
            critical_path = result_data.get("criticalPath", [])

            gantt_tasks: list[GanttTask] = []
            for i, item in enumerate(items):
                gantt_tasks.append(GanttTask(
                    id=item.get("id", f"gt_{i}"),
                    title=item.get("title", item.get("name", f"Task {i}")),
                    startDate=item.get("startDate", item.get("start", "")),
                    endDate=item.get("endDate", item.get("end", "")),
                    durationDays=item.get("durationDays", item.get("duration", 0)),
                    progress=item.get("progress", 0),
                    dependencies=item.get("dependencies", []),
                    assignee=item.get("assignee"),
                    milestoneId=item.get("milestoneId"),
                    color=item.get("color", "#4A90D9"),
                ))

            return PMPhaseResult(
                phase=PMPhase.GANTT, status=PMPhaseStatus.COMPLETED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                data={
                    "ganttTasks": [g.model_dump(mode="json", by_alias=True) for g in gantt_tasks],
                    "criticalPath": critical_path,
                    "summary": result_data.get("summary", ""),
                    "raw": result_data,
                },
            )
        except Exception as exc:
            logger.exception("Gantt(skill) failed workflow_id=%s", state.workflow_id)
            return PMPhaseResult(
                phase=PMPhase.GANTT, status=PMPhaseStatus.FAILED,
                startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                error={"code": "GANTT_SKILL_FAILED", "message": str(exc)},
            )


gantt_node = GanttCallNode()
