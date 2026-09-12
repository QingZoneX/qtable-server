from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from app.agent_workflow.state import (
    WorkflowAgentState,
    WorkflowStepStatus,
)
from app.agent_workflow.nodes.planner import planner_node
from app.agent_workflow.nodes.tool_node import tool_execution_node
from app.agent_workflow.nodes.observation import observation_node
from app.agent_workflow.nodes.retry import retry_node
from app.agent_workflow.nodes.human_approval import human_approval_node

logger = logging.getLogger(__name__)


class AgentWorkflowGraph:
    def __init__(self, checkpointer=None) -> None:
        self._graph: Optional[StateGraph] = None
        self._compiled = None
        self._checkpointer = checkpointer or MemorySaver()

    def build(self) -> StateGraph:
        graph = StateGraph(WorkflowAgentState)

        graph.add_node("planner", self._planning_node)
        graph.add_node("tool_executor", self._tool_executor_node)
        graph.add_node("observer", self._observer_node)
        graph.add_node("retry_handler", self._retry_handler_node)
        graph.add_node("human_approval", self._human_approval_node)
        graph.add_node("finalize", self._finalize_node)

        graph.set_entry_point("planner")

        graph.add_conditional_edges(
            "planner",
            self._route_after_planning,
            {
                "execute": "tool_executor",
                "finalize": "finalize",
                "await_approval": "human_approval",
            },
        )

        graph.add_conditional_edges(
            "tool_executor",
            self._route_after_tool,
            {
                "observe": "observer",
                "retry": "retry_handler",
                "approval": "human_approval",
                "continue": "tool_executor",
                "finalize": "finalize",
            },
        )

        graph.add_conditional_edges(
            "observer",
            self._route_after_observation,
            {
                "execute": "tool_executor",
                "retry": "retry_handler",
                "replan": "planner",
                "finalize": "finalize",
            },
        )

        graph.add_conditional_edges(
            "retry_handler",
            self._route_after_retry,
            {
                "execute": "tool_executor",
                "observe": "observer",
                "finalize": "finalize",
            },
        )

        graph.add_edge("human_approval", END)

        graph.add_edge("finalize", END)

        self._graph = graph
        self._compiled = graph.compile(checkpointer=self._checkpointer)
        return graph

    @property
    def compiled(self):
        if self._compiled is None:
            self.build()
        return self._compiled

    async def _planning_node(self, state: WorkflowAgentState) -> WorkflowAgentState:
        logger.info("PlanningNode start workflow_id=%s", state.workflow_id)
        state.status = "planning"

        model = state.metadata.get("_model")
        if model is None:
            state.status = "failed"
            state.error = {"code": "NO_MODEL", "message": "No LLM model available for planning"}
            return state

        try:
            plan = await planner_node.plan(state, model)
            state.plan = plan
            state.current_step_index = -1
            state.status = "executing"
            logger.info("PlanningNode completed workflow_id=%s steps=%d", state.workflow_id, len(plan.steps))
        except Exception as exc:
            logger.exception("PlanningNode failed workflow_id=%s", state.workflow_id)
            state.status = "failed"
            state.error = {"code": "PLANNING_FAILED", "message": str(exc)}

        return state

    async def _tool_executor_node(self, state: WorkflowAgentState) -> WorkflowAgentState:
        if not state.plan or not state.plan.steps:
            state.status = "completed"
            return state

        next_idx = self._find_next_step(state)
        if next_idx is None:
            state.status = "completed"
            return state

        state.current_step_index = next_idx

        workspace_id = state.workspace_id
        try:
            await tool_execution_node.execute_step(state, next_idx, workspace_id)
        except Exception as exc:
            logger.exception("ToolExecutorNode failed workflow_id=%s step=%d", state.workflow_id, next_idx)
            step = state.plan.steps[next_idx]
            step.status = WorkflowStepStatus.FAILED
            step.error = {"code": "NODE_EXECUTION_ERROR", "message": str(exc)}

        return state

    async def _observer_node(self, state: WorkflowAgentState) -> WorkflowAgentState:
        try:
            return await observation_node.observe(state)
        except Exception as exc:
            logger.exception("ObserverNode failed workflow_id=%s", state.workflow_id)
            state.status = "observing"
        return state

    async def _retry_handler_node(self, state: WorkflowAgentState) -> WorkflowAgentState:
        try:
            return await retry_node.evaluate_retry(state)
        except Exception as exc:
            logger.exception("RetryHandlerNode failed workflow_id=%s", state.workflow_id)
            state.status = "failed"
            state.error = {"code": "RETRY_FAILED", "message": str(exc)}
        return state

    async def _human_approval_node(self, state: WorkflowAgentState) -> WorkflowAgentState:
        try:
            return await human_approval_node.check_approvals(state)
        except Exception as exc:
            logger.exception("HumanApprovalNode failed workflow_id=%s", state.workflow_id)
            state.status = "awaiting_approval"
        return state

    async def _finalize_node(self, state: WorkflowAgentState) -> WorkflowAgentState:
        logger.info("FinalizeNode start workflow_id=%s status=%s", state.workflow_id, state.status)
        state.updated_at = datetime.now(timezone.utc).isoformat()

        if state.status not in {"completed", "failed", "awaiting_approval", "cancelled"}:
            state.status = "completed"

        if state.tool_calls:
            last_call = state.tool_calls[-1]
            if last_call.output and last_call.state == "completed":
                state.final_response = last_call.output.get("answer") or last_call.output.get("summary") or ""

        if not state.final_response and state.observations:
            state.final_response = state.observations[-1].summary

        if not state.final_response:
            state.final_response = "工作流执行完成"

        logger.info("FinalizeNode completed workflow_id=%s final_response_len=%d", state.workflow_id, len(state.final_response or ""))
        return state

    def _route_after_planning(
        self, state: WorkflowAgentState
    ) -> Literal["execute", "finalize", "await_approval"]:
        if state.status == "failed":
            return "finalize"
        if not state.plan or not state.plan.steps:
            return "finalize"
        if state.pending_approvals:
            return "await_approval"
        return "execute"

    def _route_after_tool(
        self, state: WorkflowAgentState
    ) -> Literal["observe", "retry", "approval", "continue", "finalize"]:
        if state.status == "awaiting_approval":
            return "approval"
        if state.status == "failed":
            return "finalize"

        current_idx = state.current_step_index
        if current_idx < 0 or not state.plan or current_idx >= len(state.plan.steps):
            next_pending = self._find_next_step(state)
            if next_pending is not None:
                state.current_step_index = next_pending
                return "continue"
            return "finalize"

        step = state.plan.steps[current_idx]

        if step.status == WorkflowStepStatus.FAILED:
            return "retry"
        if step.status == WorkflowStepStatus.REQUIRES_APPROVAL:
            return "approval"
        if step.status == WorkflowStepStatus.COMPLETED:
            return "observe"

        next_pending = self._find_next_step(state)
        if next_pending is not None:
            state.current_step_index = next_pending
            return "continue"

        return "finalize"

    def _route_after_observation(
        self, state: WorkflowAgentState
    ) -> Literal["execute", "retry", "replan", "finalize"]:
        if state.status == "failed":
            return "finalize"
        if state.status == "awaiting_approval":
            return "finalize"

        all_steps_completed = (
            state.plan
            and all(
                s.status in {WorkflowStepStatus.COMPLETED, WorkflowStepStatus.SKIPPED}
                for s in state.plan.steps
            )
        )
        if all_steps_completed:
            return "finalize"

        has_failures = (
            state.plan
            and any(s.status == WorkflowStepStatus.FAILED for s in state.plan.steps)
        )
        if has_failures:
            current_idx = state.current_step_index
            if current_idx >= 0 and current_idx < len(state.plan.steps):
                step = state.plan.steps[current_idx]
                if step.status == WorkflowStepStatus.FAILED and step.retry_count < step.max_retries:
                    return "retry"
            return "replan"

        next_pending = self._find_next_step(state)
        if next_pending is not None:
            return "execute"

        return "finalize"

    def _route_after_retry(
        self, state: WorkflowAgentState
    ) -> Literal["execute", "observe", "finalize"]:
        if state.status == "failed":
            return "finalize"
        if state.status == "awaiting_approval":
            return "finalize"
        if state.status == "executing":
            return "execute"
        if state.status == "observing":
            return "observe"
        if state.status == "completed":
            return "finalize"
        return "finalize"

    def _find_next_step(self, state: WorkflowAgentState) -> int | None:
        if not state.plan or not state.plan.steps:
            return None

        completed_ids = {
            s.step_id
            for s in state.plan.steps
            if s.status == WorkflowStepStatus.COMPLETED
        }

        for idx, step in enumerate(state.plan.steps):
            if step.status != WorkflowStepStatus.PENDING:
                continue
            if all(dep in completed_ids for dep in step.depends_on):
                return idx

        for idx, step in enumerate(state.plan.steps):
            if step.status == WorkflowStepStatus.PENDING:
                return idx

        return None


agent_workflow_graph = AgentWorkflowGraph()


def build_agent_workflow(checkpointer=None):
    graph = AgentWorkflowGraph(checkpointer=checkpointer)
    return graph.build()
