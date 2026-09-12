from __future__ import annotations

import logging
from typing import Optional

from app.agent_runtime.types import AgentState

logger = logging.getLogger(__name__)


class AgentStateMachine:
    _transitions: dict[AgentState, dict[str, AgentState]] = {
        AgentState.IDLE: {
            "start": AgentState.PLANNING,
        },
        AgentState.PLANNING: {
            "need_preview": AgentState.PREVIEW,
            "direct_execute": AgentState.EXECUTING,
            "direct_answer": AgentState.COMPLETED,
            "failed": AgentState.FAILED,
        },
        AgentState.PREVIEW: {
            "show_preview": AgentState.AWAITING_CONFIRM,
            "no_confirm_needed": AgentState.EXECUTING,
            "failed": AgentState.FAILED,
        },
        AgentState.AWAITING_CONFIRM: {
            "approved": AgentState.EXECUTING,
            "rejected": AgentState.COMPLETED,
            "timeout": AgentState.STOPPED,
            "cancelled": AgentState.STOPPED,
        },
        AgentState.EXECUTING: {
            "step_complete": AgentState.OBSERVING,
            "all_complete": AgentState.COMPLETED,
            "failed": AgentState.FAILED,
            "stopped": AgentState.STOPPED,
        },
        AgentState.OBSERVING: {
            "continue": AgentState.EXECUTING,
            "replan": AgentState.PLANNING,
            "all_complete": AgentState.COMPLETED,
            "failed": AgentState.FAILED,
        },
        AgentState.COMPLETED: {},
        AgentState.FAILED: {
            "retry": AgentState.PLANNING,
        },
        AgentState.STOPPED: {},
    }

    def __init__(self) -> None:
        self.state: AgentState = AgentState.IDLE
        self._previous_state: Optional[AgentState] = None

    def can_transition(self, event: str) -> bool:
        return event in self._transitions.get(self.state, {})

    def transition(self, event: str) -> AgentState:
        if not self.can_transition(event):
            raise ValueError(
                f"Invalid transition: {self.state.value} -> {event}"
            )
        self._previous_state = self.state
        next_state = self._transitions[self.state][event]
        self.state = next_state
        logger.info(
            "AgentStateMachine transition: %s -> %s (event=%s)",
            self._previous_state.value,
            self.state.value,
            event,
        )
        return next_state

    @property
    def previous_state(self) -> Optional[AgentState]:
        return self._previous_state

    def reset(self) -> None:
        self._previous_state = self.state
        self.state = AgentState.IDLE
        logger.info("AgentStateMachine reset to IDLE")
