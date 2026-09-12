from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.my_work import (
    MyWorkValidationError,
    import_recent_targets,
    remove_recent_target,
    upsert_recent_target,
)


@strawberry.type
class MyWorkMutations:
    @strawberry.mutation(name="upsertRecentTarget")
    async def upsert_recent_target_mutation(
        self,
        info: Info,
        target: JSON,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("My Work requires database backend")
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        try:
            return await upsert_recent_target(db, user_id=user.id, raw_target=target)
        except MyWorkValidationError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="importRecentTargets")
    async def import_recent_targets_mutation(
        self,
        info: Info,
        targets: List[JSON],
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("My Work requires database backend")
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        try:
            return await import_recent_targets(db, user_id=user.id, raw_targets=targets)
        except MyWorkValidationError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.mutation(name="removeRecentTarget")
    async def remove_recent_target_mutation(
        self,
        info: Info,
        entity_type: str,
        entity_id: str,
        table_id: Optional[str] = None,
    ) -> bool:
        if _resolve_backend(None) != "db":
            raise GraphQLError("My Work requires database backend")
        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        return await remove_recent_target(
            db,
            user_id=user.id,
            entity_type=entity_type,
            entity_id=entity_id,
            table_id=table_id,
        )
