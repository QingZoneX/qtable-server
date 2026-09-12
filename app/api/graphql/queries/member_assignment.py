from __future__ import annotations

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user
from app.services.member_assignment import member_assignment_service


@strawberry.type
class MemberAssignmentQueries:
    @strawberry.field(name="memberAssignmentBatch")
    async def member_assignment_batch(
        self,
        info: Info,
        batch_id: str,
    ) -> JSON | None:
        user = await _require_user(info)
        return await member_assignment_service.get_batch(
            info.context["db"],
            user_id=user.id,
            batch_id=batch_id,
        )
