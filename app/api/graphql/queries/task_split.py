from __future__ import annotations

import strawberry
from sqlalchemy import select
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.api.graphql.types import TaskSplitResultInfo
from app.models.task_split import TaskSplitPlan
from app.services.task_planning import task_planning_service


@strawberry.type
class TaskSplitQueries:
    @strawberry.field(name="taskSplitPlan")
    async def task_split_plan(
        self,
        info: Info,
        planId: str,
    ) -> TaskSplitResultInfo | None:
        user = await _require_user(info)
        db = info.context["db"]
        result = await db.execute(
            select(TaskSplitPlan)
            .where(
                TaskSplitPlan.id == planId,
                TaskSplitPlan.created_by == str(user.id),
            )
            .limit(1)
        )
        plan = result.scalars().first()
        if not plan:
            return None
        return TaskSplitResultInfo(
            traceId=plan.trace_id or "",
            provider=plan.provider or "",
            model=plan.model or "",
            attempts=1,
            persisted=True,
            planId=plan.id,
            result=plan.structured_output,
            error=None,
            createdAt=plan.created_at.isoformat() if plan.created_at else None,
        )


    @strawberry.field(name="taskPlanningPlan")
    async def task_planning_plan(
        self,
        info: Info,
        plan_id: str,
    ) -> JSON | None:
        user = await _require_user(info)
        db = info.context["db"]
        return await task_planning_service.get_plan(
            db,
            user_id=user.id,
            plan_id=plan_id,
        )
