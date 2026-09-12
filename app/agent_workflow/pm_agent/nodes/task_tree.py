from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseResult, PMPhaseStatus, TaskTreeNode,
)
from app.skills.contracts import SkillCallRequest, SkillContext
from app.skills.runtime import SkillExecutionContext, SkillRuntime
from app.services.skill_registry import skill_registry_service

logger = logging.getLogger(__name__)


def _parse_task_tree(task_data: dict[str, Any]) -> TaskTreeNode:
    children = [_parse_task_tree(c) for c in task_data.get("children", [])]
    return TaskTreeNode(
        title=task_data.get("title", ""),
        description=task_data.get("description", ""),
        moduleId=task_data.get("moduleId"),
        parentId=task_data.get("parentId"),
        depth=task_data.get("depth", 0),
        estimateHours=task_data.get("estimateHours", 0),
        priority=task_data.get("priority", "medium"),
        dependsOn=task_data.get("dependsOn", []),
        children=children,
    )


class TaskTreeCallNode:
    """Calls qtable.task.split skill to decompose modules into a task tree."""

    async def build(
        self,
        state: PMAgentState,
        db: AsyncSession,
        user_id: int,
        workspace_id: str | None,
    ) -> PMPhaseResult:
        logger.info("TaskTree(skill) start workflow_id=%s", state.workflow_id)
        started = datetime.now(timezone.utc).isoformat()
        try:
            registry = await skill_registry_service.build_runtime_registry(db, workspace_id=workspace_id)
            runtime = SkillRuntime(registry)

            skill_def = registry.get("qtable.task.split")
            if skill_def is None:
                raise RuntimeError("qtable.task.split skill not found in registry, register it first")

            prompt = state.user_message
            if state.requirements:
                prompt += "\n\n需求:\n" + "\n".join(
                    f"- [{r.priority}] {r.title}: {r.description}" for r in state.requirements
                )
            if state.modules:
                prompt += "\n\n模块:\n" + "\n".join(
                    f"- {m.name}: {m.description}" for m in state.modules
                )

            execution_context = SkillExecutionContext(
                context=SkillContext(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    session_id=state.session_id,
                    conversation_id=state.conversation_id,
                    project_id=state.project_id,
                    table_ids=state.table_ids,
                    view_id=state.view_id,
                    task_id=state.task_id,
                    team_id=state.team_id,
                    organization_id=state.organization_id,
                    workflow_id=state.workflow_id,
                    agent_id=state.agent_id,
                    trace_id=state.workflow_id,
                    origin="workflow",
                    locale=state.locale,
                    timezone=state.timezone,
                    dry_run=False,
                    confirmed=True,
                ),
                db=db,
            )

            response = await runtime.invoke(
                SkillCallRequest(
                    call_id=state.workflow_id,
                    skill_name="qtable.task.split",
                    input={
                        "prompt": prompt,
                        "autoCreateRecords": False,
                        "dryRun": False,
                        "maxDepth": 3,
                        "workspaceId": workspace_id,
                        "locale": state.locale,
                        "timezone": state.timezone,
                    },
                    dry_run=False,
                    confirmed=True,
                    origin="workflow",
                    trace_id=state.workflow_id,
                ),
                execution_context,
            )

            if response.error:
                return PMPhaseResult(
                    phase=PMPhase.TASK_TREE,
                    status=PMPhaseStatus.FAILED,
                    startedAt=started,
                    completedAt=datetime.now(timezone.utc).isoformat(),
                    error={"code": response.error.code.value, "message": response.error.message},
                )

            result_data = response.output or {}
            plan = result_data.get("result", {})
            root = plan.get("root", {})

            root_tasks: list[TaskTreeNode] = []
            if root:
                root_tasks = [_parse_task_tree(root)]

            all_tasks: list[TaskTreeNode] = []
            def flatten(node: TaskTreeNode):
                all_tasks.append(node)
                for child in node.children:
                    flatten(child)
            for t in root_tasks:
                flatten(t)

            return PMPhaseResult(
                phase=PMPhase.TASK_TREE,
                status=PMPhaseStatus.COMPLETED,
                startedAt=started,
                completedAt=datetime.now(timezone.utc).isoformat(),
                data={
                    "allTasks": [t.model_dump(mode="json", by_alias=True) for t in all_tasks],
                    "rootTasks": [t.model_dump(mode="json", by_alias=True) for t in root_tasks],
                    "plan": plan,
                    "mermaid": plan.get("mermaid", ""),
                    "summary": plan.get("summary", ""),
                    "totalLeafTasks": len([t for t in all_tasks if not t.children]),
                    "totalEstimatedHours": sum(t.estimateHours for t in all_tasks),
                },
            )
        except Exception as exc:
            logger.exception("TaskTree(skill) failed workflow_id=%s", state.workflow_id)
            return PMPhaseResult(
                phase=PMPhase.TASK_TREE,
                status=PMPhaseStatus.FAILED,
                startedAt=started,
                completedAt=datetime.now(timezone.utc).isoformat(),
                error={"code": "TASK_TREE_SKILL_FAILED", "message": str(exc)},
            )


task_tree_node = TaskTreeCallNode()
