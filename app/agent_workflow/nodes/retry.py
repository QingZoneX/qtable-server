from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.agent_workflow.state import (
    WorkflowAgentState,
    WorkflowStepStatus,
)

logger = logging.getLogger(__name__)


class RetryNode:
    async def evaluate_retry(self, state: WorkflowAgentState) -> WorkflowAgentState:
        state.status = "retrying"

        if not state.plan or not state.plan.steps:
            state.status = "failed"
            state.error = {"code": "NO_PLAN", "message": "No plan to retry"}
            return state

        current_idx = state.current_step_index
        if current_idx < 0 or current_idx >= len(state.plan.steps):
            state.status = "failed"
            state.error = {"code": "INVALID_STEP_INDEX", "message": f"Invalid step index: {current_idx}"}
            return state

        step = state.plan.steps[current_idx]

        if step.status == WorkflowStepStatus.FAILED:
            if step.retry_count < step.max_retries:
                retry_delay = step.retry_delay_ms * (2 ** (step.retry_count - 1)) / 1000
                retry_delay = min(retry_delay, 30.0)

                step.status = WorkflowStepStatus.PENDING
                step.error = None

                logger.info(
                    "RetryNode retrying step=%d workflow_id=%s attempt=%d/%d delay=%.1fs",
                    current_idx,
                    state.workflow_id,
                    step.retry_count + 1,
                    step.max_retries,
                    retry_delay,
                )

                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)

                state.status = "executing"
            else:
                logger.warning(
                    "RetryNode exhausted step=%d workflow_id=%s attempts=%d",
                    current_idx,
                    state.workflow_id,
                    step.retry_count,
                )
                step.status = WorkflowStepStatus.FAILED
                state.status = "failed"
                state.error = {
                    "code": "MAX_RETRIES_EXCEEDED",
                    "message": f"Step {current_idx} ({step.skill_name}) failed after {step.retry_count} attempts",
                }

        elif step.status == WorkflowStepStatus.REQUIRES_APPROVAL:
            state.status = "awaiting_approval"

        else:
            next_pending = self._find_next_pending_step(state)
            if next_pending is not None:
                state.current_step_index = next_pending
                state.status = "executing"
            else:
                all_done = all(
                    s.status in {WorkflowStepStatus.COMPLETED, WorkflowStepStatus.SKIPPED}
                    for s in state.plan.steps
                )
                if all_done:
                    state.status = "completed"
                elif state.pending_approvals:
                    state.status = "awaiting_approval"
                else:
                    state.status = "completed"

        return state

    def _find_next_pending_step(self, state: WorkflowAgentState) -> int | None:
        if not state.plan:
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


retry_node = RetryNode()
