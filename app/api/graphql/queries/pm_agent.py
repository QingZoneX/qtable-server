from typing import List, Optional, AsyncGenerator

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.types import (
    PMAgentStatusResponse,
    PMAgentListResponse,
    PMAgentRunResponse,
    PMAgentRunInput,
    PMAgentPhaseInfo,
)
from app.api.graphql.helpers import _require_item_permission
from app.agent_workflow.pm_agent.service import (
    pm_agent_service,
    PMAgentRunRequest,
)
from app.context_engine import ContextBuildInput, context_builder
from app.services.ai_tool_router import tool_router_service
from app.agent_workflow.persistence import (
    workflow_persistence_manager,
    redis_session_manager,
)

import logging

logger = logging.getLogger(__name__)


@strawberry.type
class PMAgentQueries:
    @strawberry.field
    async def pm_agent_status(
        self,
        info: Info,
        workflow_id: str,
    ) -> Optional[PMAgentStatusResponse]:
        db: AsyncSession = info.context["db"]
        user_id = info.context.get("user", {}).get("id")

        if not user_id:
            return None

        status = await pm_agent_service.get_status(db, workflow_id, user_id)
        if not status:
            return None

        return PMAgentStatusResponse(
            workflowId=strawberry.ID(status.workflow_id),
            status=status.status,
            pmPhase=status.pm_phase,
            phaseSequence=status.phase_sequence,
            completedPhases=status.completed_phases,
            totalPhases=status.total_phases,
            phaseResults=[
                PMAgentPhaseInfo(
                    phase=pr.get("phase", ""),
                    status=pr.get("status", ""),
                    startedAt=pr.get("startedAt"),
                    completedAt=pr.get("completedAt"),
                    data=pr.get("data"),
                    error=pr.get("error"),
                )
                for pr in status.phase_results
            ],
            finalResponse=status.final_response,
            error=status.error,
            createdAt=status.created_at,
            updatedAt=status.updated_at,
        )

    @strawberry.field
    async def pm_agent_list(
        self,
        info: Info,
        workspace_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> PMAgentListResponse:
        db: AsyncSession = info.context["db"]
        user_id = info.context.get("user", {}).get("id")

        if not user_id:
            return PMAgentListResponse(workflows=[], total=0, offset=offset, limit=limit)

        try:
            redis = await redis_session_manager.client
            if workspace_id:
                session_workflows = await redis_session_manager.get_session_workflows(str(user_id))
            else:
                session_workflows = []
        except Exception:
            session_workflows = []

        workflows: list[PMAgentStatusResponse] = []
        for wf_id in session_workflows[offset:offset + limit]:
            status = await pm_agent_service.get_status(db, wf_id, user_id)
            if status:
                workflows.append(PMAgentStatusResponse(
                    workflowId=strawberry.ID(status.workflow_id),
                    status=status.status,
                    pmPhase=status.pm_phase,
                    phaseSequence=status.phase_sequence,
                    completedPhases=status.completed_phases,
                    totalPhases=status.total_phases,
                    finalResponse=status.final_response,
                    createdAt=status.created_at,
                    updatedAt=status.updated_at,
                ))

        return PMAgentListResponse(
            workflows=workflows,
            total=len(session_workflows),
            offset=offset,
            limit=limit,
        )
