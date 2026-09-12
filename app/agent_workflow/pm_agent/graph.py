from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from app.agent_workflow.pm_agent.state import (
    PMAgentState, PMPhase, PMPhaseStatus, PMPhaseResult,
    RequirementItem, ModuleItem, TaskTreeNode, WorkloadEstimateItem,
    MemberAssignment, MilestoneItem, GanttTask, RiskItem, ProjectReport,
)
from app.agent_workflow.pm_agent.nodes.planner import pm_planner_node
from app.agent_workflow.pm_agent.nodes.requirement_analysis import requirement_analysis_node
from app.agent_workflow.pm_agent.nodes.module_split import module_split_node
from app.agent_workflow.pm_agent.nodes.task_tree import task_tree_node
from app.agent_workflow.pm_agent.nodes.workload_estimate import workload_estimate_node
from app.agent_workflow.pm_agent.nodes.member_assignment import member_assignment_node
from app.agent_workflow.pm_agent.nodes.milestone import milestone_node
from app.agent_workflow.pm_agent.nodes.gantt import gantt_node
from app.agent_workflow.pm_agent.nodes.risk_analysis import risk_analysis_node
from app.agent_workflow.pm_agent.nodes.workflow_design import workflow_design_node
from app.agent_workflow.pm_agent.nodes.report import report_node

logger = logging.getLogger(__name__)

PHASE_ORDER: list[PMPhase] = list(PMPhase)

PHASE_NODE_MAP: dict[PMPhase, str] = {
    PMPhase.REQUIREMENT_ANALYSIS: "requirement_analysis",
    PMPhase.MODULE_SPLIT: "module_split",
    PMPhase.TASK_TREE: "task_tree",
    PMPhase.WORKLOAD_ESTIMATE: "workload_estimate",
    PMPhase.MEMBER_ASSIGNMENT: "member_assignment",
    PMPhase.MILESTONE: "milestone",
    PMPhase.GANTT: "gantt",
    PMPhase.RISK_ANALYSIS: "risk_analysis",
    PMPhase.WORKFLOW_DESIGN: "workflow_design",
    PMPhase.REPORT: "report",
}

ALL_PHASE_NODES = [
    "requirement_analysis", "module_split", "task_tree", "workload_estimate",
    "member_assignment", "milestone", "gantt", "risk_analysis", "workflow_design", "report",
]


def _next_phase_node(current: PMPhase) -> str:
    try:
        idx = PHASE_ORDER.index(current)
        if idx + 1 < len(PHASE_ORDER):
            return PHASE_NODE_MAP[PHASE_ORDER[idx + 1]]
    except (ValueError, KeyError):
        pass
    return "finalize"


class PMAgentGraph:
    def __init__(self, checkpointer=None) -> None:
        self._graph: Optional[StateGraph] = None
        self._compiled = None
        self._checkpointer = checkpointer or MemorySaver()

    def build(self) -> StateGraph:
        graph = StateGraph(PMAgentState)

        graph.add_node("pm_planner", self._pm_planning_node)
        for node_name in ALL_PHASE_NODES:
            graph.add_node(node_name, self._make_phase_runner(node_name))
        graph.add_node("finalize", self._finalize_node)

        graph.set_entry_point("pm_planner")

        graph.add_conditional_edges("pm_planner", self._route_from_planner, {
            n: n for n in ALL_PHASE_NODES
        } | {"finalize": "finalize"})

        for node_name in ALL_PHASE_NODES:
            graph.add_conditional_edges(node_name, self._route_after_phase,
                                        {n: n for n in ALL_PHASE_NODES} | {"finalize": "finalize"})

        graph.add_edge("finalize", END)

        self._graph = graph
        self._compiled = graph.compile(checkpointer=self._checkpointer)
        return graph

    @property
    def compiled(self):
        if self._compiled is None:
            self.build()
        return self._compiled

    def _get_model(self, s: PMAgentState): return s.metadata.get("_model")
    def _get_db(self, s: PMAgentState): return s.metadata.get("_db")

    def _make_phase_runner(self, node_name: str):
        _reverse = {v: k for k, v in PHASE_NODE_MAP.items()}
        phase = _reverse[node_name]

        async def _run(state: PMAgentState) -> PMAgentState:
            logger.info("PMAgent phase=%s node=%s workflow_id=%s", phase.value, node_name, state.workflow_id)
            state.pm_phase = phase
            state.status = "executing"
            started = datetime.now(timezone.utc).isoformat()

            try:
                if phase == PMPhase.TASK_TREE or phase == PMPhase.WORKLOAD_ESTIMATE or phase == PMPhase.GANTT:
                    result = await self._execute_skill_phase(state, phase)
                else:
                    result = await self._execute_standalone_phase(state, phase)

                state.phase_results.append(result)
                if result.status == PMPhaseStatus.COMPLETED and result.data:
                    self._merge_phase_data(state, phase, result.data)
                if result.status == PMPhaseStatus.COMPLETED:
                    state.completed_phases = sum(
                        1 for pr in state.phase_results if pr.status == PMPhaseStatus.COMPLETED
                    )
                logger.info("PMAgent phase=%s done status=%s", phase.value, result.status.value)
            except Exception as exc:
                logger.exception("PMAgent phase=%s crashed", phase.value)
                state.phase_results.append(PMPhaseResult(
                    phase=phase, status=PMPhaseStatus.FAILED,
                    startedAt=started, completedAt=datetime.now(timezone.utc).isoformat(),
                    error={"code": "PHASE_CRASH", "message": str(exc)},
                ))
            return state
        return _run

    async def _execute_standalone_phase(self, state: PMAgentState, phase: PMPhase) -> PMPhaseResult:
        model = self._get_model(state)
        if model is None:
            return PMPhaseResult(phase=phase, status=PMPhaseStatus.FAILED,
                                 error={"code": "NO_MODEL", "message": "No LLM model available"})

        if phase == PMPhase.REQUIREMENT_ANALYSIS:
            return await requirement_analysis_node.analyze(state, model)
        elif phase == PMPhase.MODULE_SPLIT:
            return await module_split_node.split(state, model)
        elif phase == PMPhase.MEMBER_ASSIGNMENT:
            return await member_assignment_node.assign(state, model)
        elif phase == PMPhase.MILESTONE:
            return await milestone_node.plan(state, model)
        elif phase == PMPhase.RISK_ANALYSIS:
            return await risk_analysis_node.analyze(state, model)
        elif phase == PMPhase.WORKFLOW_DESIGN:
            return await workflow_design_node.design(state, model)
        elif phase == PMPhase.REPORT:
            return await report_node.generate(state, model)
        else:
            return PMPhaseResult(phase=phase, status=PMPhaseStatus.SKIPPED)

    async def _execute_skill_phase(self, state: PMAgentState, phase: PMPhase) -> PMPhaseResult:
        db = self._get_db(state)
        user_id = state.user_id
        if db is None or user_id is None:
            return PMPhaseResult(phase=phase, status=PMPhaseStatus.FAILED,
                                 error={"code": "MISSING_CONTEXT", "message": "db or user_id not available"})
        if phase == PMPhase.TASK_TREE:
            return await task_tree_node.build(state, db, user_id, state.workspace_id)
        elif phase == PMPhase.WORKLOAD_ESTIMATE:
            return await workload_estimate_node.estimate(state, db, user_id, state.workspace_id)
        elif phase == PMPhase.GANTT:
            return await gantt_node.generate(state, db, user_id, state.workspace_id)
        return PMPhaseResult(phase=phase, status=PMPhaseStatus.SKIPPED)

    async def _pm_planning_node(self, state: PMAgentState) -> PMAgentState:
        logger.info("PMAgent Planner start workflow_id=%s", state.workflow_id)
        state.status = "planning"
        model = self._get_model(state)
        if model is None:
            state.status = "failed"
            state.error = {"code": "NO_MODEL", "message": "No LLM model available"}
            return state
        try:
            phases = await pm_planner_node.plan(state, model)
            state.phase_sequence = phases
            state.total_phases = len(phases)
            state.current_phase_index = 0
            state.pm_phase = phases[0] if phases else None
            state.phase_results = []
            state.completed_phases = 0
            state.status = "executing"
            logger.info("PMAgent Planner: %d phases", len(phases))
        except Exception as exc:
            logger.exception("PMAgent Planner failed")
            state.status = "failed"
            state.error = {"code": "PM_PLANNING_FAILED", "message": str(exc)}
        return state

    def _route_from_planner(self, state: PMAgentState) -> str:
        if state.status == "failed" or not state.pm_phase:
            return "finalize"
        return PHASE_NODE_MAP.get(state.pm_phase, "finalize")

    def _route_after_phase(self, state: PMAgentState) -> str:
        if state.status == "failed":
            return "finalize"
        if not state.pm_phase:
            return "finalize"
        next_node = _next_phase_node(state.pm_phase)
        if next_node == "finalize":
            return "finalize"
        if next_node in PHASE_NODE_MAP.values():
            return next_node
        return "finalize"

    async def _finalize_node(self, state: PMAgentState) -> PMAgentState:
        logger.info("PMAgent Finalize workflow_id=%s status=%s", state.workflow_id, state.status)
        state.updated_at = datetime.now(timezone.utc).isoformat()
        if state.status not in {"completed", "failed", "awaiting_approval", "cancelled"}:
            state.status = "completed"
        if state.project_report:
            state.final_response = state.project_report.executive_summary
        else:
            completed = sum(1 for pr in state.phase_results if pr.status == PMPhaseStatus.COMPLETED)
            state.final_response = f"项目规划完成，共完成 {completed}/{state.total_phases} 个阶段"
        return state

    def _merge_phase_data(self, state: PMAgentState, phase: PMPhase, data: dict[str, Any]) -> None:
        if phase == PMPhase.REQUIREMENT_ANALYSIS and "requirements" in data:
            state.requirements = [RequirementItem.model_validate(r) if isinstance(r, dict) else r for r in data["requirements"]]
        elif phase == PMPhase.MODULE_SPLIT and "modules" in data:
            state.modules = [ModuleItem.model_validate(m) if isinstance(m, dict) else m for m in data["modules"]]
        elif phase == PMPhase.TASK_TREE and "allTasks" in data:
            state.task_tree = [TaskTreeNode.model_validate(t) if isinstance(t, dict) else t for t in data["allTasks"]]
        elif phase == PMPhase.WORKLOAD_ESTIMATE and "estimates" in data:
            state.workload_estimates = [WorkloadEstimateItem.model_validate(e) if isinstance(e, dict) else e for e in data["estimates"]]
        elif phase == PMPhase.MEMBER_ASSIGNMENT and "assignments" in data:
            state.member_assignments = [MemberAssignment.model_validate(a) if isinstance(a, dict) else a for a in data["assignments"]]
        elif phase == PMPhase.MILESTONE and "milestones" in data:
            state.milestones = [MilestoneItem.model_validate(m) if isinstance(m, dict) else m for m in data["milestones"]]
        elif phase == PMPhase.GANTT and "ganttTasks" in data:
            state.gantt_tasks = [GanttTask.model_validate(g) if isinstance(g, dict) else g for g in data["ganttTasks"]]
        elif phase == PMPhase.RISK_ANALYSIS and "risks" in data:
            state.risks = [RiskItem.model_validate(r) if isinstance(r, dict) else r for r in data["risks"]]
        elif phase == PMPhase.REPORT and "report" in data:
            rpt = data["report"]
            state.project_report = ProjectReport.model_validate(rpt) if isinstance(rpt, dict) else rpt


pm_agent_graph = PMAgentGraph()
