from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.agent_runtime.types import (
    ActionPreview,
    AgentState,
    IntentResult,
    ObservationRecord,
    SSERuntimeEvent,
    ToolPlan,
)

logger = logging.getLogger(__name__)


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class SSEEventEmitter:
    def __init__(self, send_queue: list[str] | None = None) -> None:
        self._events: list[str] = []
        self._send_queue = send_queue

    def emit(self, payload: dict) -> None:
        event_str = _sse_event(payload)
        self._events.append(event_str)
        if self._send_queue is not None:
            self._send_queue.append(event_str)

    def emit_state(self, state: AgentState) -> None:
        self.emit({
            "type": "state",
            "state": state.value,
            "agentState": state.value,
        })

    def emit_intent(self, intent: IntentResult) -> None:
        self.emit({
            "type": "intent",
            "intent": intent.intent.value,
            "confidence": intent.confidence,
            "reasoning": intent.reasoning,
            "suggestedSkills": intent.suggested_skills,
        })

    def emit_plan(self, plan: ToolPlan) -> None:
        self.emit({
            "type": "plan",
            "plan": plan.model_dump(mode="json", by_alias=True),
        })

    def emit_preview(self, preview: ActionPreview) -> None:
        self.emit({
            "type": "preview",
            "preview": preview.model_dump(mode="json", by_alias=True),
        })

    def emit_confirm_required(self, confirm_id: str, timeout: int) -> None:
        self.emit({
            "type": "confirm_required",
            "confirmId": confirm_id,
            "timeout": timeout,
        })

    def emit_tool_start(self, tool_call_id: str, skill_name: str, step_id: str) -> None:
        self.emit({
            "type": "tool_start",
            "toolCallId": tool_call_id,
            "skillName": skill_name,
            "stepId": step_id,
        })

    def emit_tool_progress(
        self,
        tool_call_id: str,
        progress: float,
        message: str = "",
    ) -> None:
        self.emit({
            "type": "tool_progress",
            "toolCallId": tool_call_id,
            "progress": progress,
            "message": message,
        })

    def emit_tool_end(
        self,
        tool_call_id: str,
        state: str,
        duration_ms: float,
    ) -> None:
        self.emit({
            "type": "tool_end",
            "toolCallId": tool_call_id,
            "state": state,
            "durationMs": duration_ms,
        })

    def emit_observation(self, observation: ObservationRecord) -> None:
        self.emit({
            "type": "observation",
            "observation": observation.model_dump(mode="json", by_alias=True),
        })

    def emit_text(self, text: str) -> None:
        self.emit({"type": "text", "content": text})

    def emit_error(self, message: str, code: str = "UNKNOWN_ERROR") -> None:
        self.emit({
            "type": "error",
            "error": True,
            "message": message,
            "code": code,
        })

    def emit_done(self) -> None:
        self.emit({"type": "done", "done": True})

    def get_events(self) -> list[str]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()


async def sse_event_generator(emitter: SSEEventEmitter):
    for event_str in emitter.get_events():
        yield event_str
