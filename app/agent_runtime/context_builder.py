from __future__ import annotations

import logging
from typing import Any, Optional

from app.agent_runtime.types import RuntimeConfig, RuntimeContext

logger = logging.getLogger(__name__)


class ContextBuilder:
    def build(
        self,
        *,
        user_id: int,
        user_message: str,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        project_id: Optional[str] = None,
        table_ids: Optional[list[str]] = None,
        view_id: Optional[str] = None,
        task_id: Optional[str] = None,
        team_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        agent_context: Optional[dict[str, Any]] = None,
        context_snapshot: Optional[dict[str, Any]] = None,
        allowed_skills: Optional[list[str]] = None,
        denied_skills: Optional[list[str]] = None,
        config: Optional[RuntimeConfig] = None,
    ) -> RuntimeContext:
        return RuntimeContext(
            user_id=user_id,
            user_message=user_message,
            session_id=session_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            project_id=project_id,
            table_ids=table_ids or [],
            view_id=view_id,
            task_id=task_id,
            team_id=team_id,
            organization_id=organization_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            messages=messages or [],
            agent_context=agent_context,
            context_snapshot=context_snapshot or {},
            allowed_skills=allowed_skills or [],
            denied_skills=denied_skills or [],
            config=config or RuntimeConfig(),
        )


_context_builder: ContextBuilder | None = None


def get_context_builder() -> ContextBuilder:
    global _context_builder
    if _context_builder is None:
        _context_builder = ContextBuilder()
    return _context_builder
