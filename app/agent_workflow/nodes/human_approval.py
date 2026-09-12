from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_workflow.state import (
    WorkflowAgentState,
    HumanApprovalRequest,
    WorkflowStepStatus,
)
from app.agent_workflow.persistence import workflow_persistence_manager

logger = logging.getLogger(__name__)


class HumanApprovalNode:
    async def check_approvals(self, state: WorkflowAgentState) -> WorkflowAgentState:
        if not state.plan:
            return state

        current_idx = state.current_step_index
        if current_idx < 0 or current_idx >= len(state.plan.steps):
            return state

        step = state.plan.steps[current_idx]

        if step.status == WorkflowStepStatus.REQUIRES_APPROVAL:
            approval = self._create_approval_request(state, step)
            state.pending_approvals.append(approval)
            state.status = "awaiting_approval"

            db = state.metadata.get("_db")
            if db is not None:
                try:
                    await workflow_persistence_manager.save_approval(
                        db,
                        {
                            "id": approval.approval_id,
                            "workflow_id": state.workflow_id,
                            "call_id": approval.call_id,
                            "skill_name": approval.skill_name,
                            "function_name": approval.function_name,
                            "arguments": approval.arguments,
                            "reason": approval.reason,
                            "preview": approval.preview,
                            "status": "pending",
                        },
                    )
                except Exception as exc:
                    logger.warning("Failed to persist approval: %s", exc)

            logger.info(
                "HumanApprovalNode created approval_id=%s skill=%s workflow_id=%s",
                approval.approval_id,
                approval.skill_name,
                state.workflow_id,
            )

        return state

    async def process_approval(
        self,
        state: WorkflowAgentState,
        approval_id: str,
        approved: bool,
        db: AsyncSession,
    ) -> WorkflowAgentState:
        for approval in state.pending_approvals:
            if approval.approval_id == approval_id and approval.status == "pending":
                approval.status = "approved" if approved else "rejected"
                approval.resolved_at = datetime.now(timezone.utc).isoformat()
                state.approval_history.append(approval)

                await workflow_persistence_manager.resolve_approval(
                    db,
                    approval_id,
                    "approved" if approved else "rejected",
                )

                if approved:
                    for step in state.plan.steps:
                        if step.status == WorkflowStepStatus.REQUIRES_APPROVAL:
                            step.status = WorkflowStepStatus.PENDING
                            state.status = "executing"
                            break
                else:
                    for step in state.plan.steps:
                        if step.status == WorkflowStepStatus.REQUIRES_APPROVAL:
                            step.status = WorkflowStepStatus.SKIPPED
                            break
                    state.status = "executing"

                state.pending_approvals = [
                    a for a in state.pending_approvals
                    if a.approval_id != approval_id
                ]

                logger.info(
                    "HumanApprovalNode resolved approval_id=%s approved=%s workflow_id=%s",
                    approval_id,
                    approved,
                    state.workflow_id,
                )
                break

        return state

    def _create_approval_request(
        self,
        state: WorkflowAgentState,
        step,
    ) -> HumanApprovalRequest:
        recent_call = None
        for tc in reversed(state.tool_calls):
            if tc.skill_name == step.skill_name:
                recent_call = tc
                break

        return HumanApprovalRequest(
            callId=recent_call.call_id if recent_call else str(uuid.uuid4()),
            skillName=step.skill_name or "",
            functionName=step.skill_name or "",
            arguments=step.arguments,
            reason=f"步骤 {step.index}: {step.description}",
            preview=recent_call.metadata if recent_call else {},
        )


human_approval_node = HumanApprovalNode()
