from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _require_item_permission,
    _resolve_backend,
    _resolve_db_table_id,
)
from app.services.task_profile import (
    TaskProfileValidationError,
    serialize_task_profile,
    suggest_task_profile,
)


def _require_task_profile_db(table_id: str) -> str:
    if _resolve_backend(table_id) != "db":
        raise GraphQLError("Task profile requires database backend")
    return _resolve_db_table_id(table_id)


@strawberry.type
class TaskProfileQueries:
    @strawberry.field(name="taskProfile")
    async def task_profile(
        self,
        info: Info,
        table_id: str,
    ) -> Optional[JSON]:
        db: AsyncSession = info.context["db"]
        resolved_table_id = _require_task_profile_db(table_id)
        await _require_item_permission(info, resolved_table_id, "read")
        try:
            return await serialize_task_profile(db, resolved_table_id)
        except TaskProfileValidationError as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="suggestTaskProfile")
    async def suggest_task_profile_query(
        self,
        info: Info,
        table_id: str,
    ) -> JSON:
        """Suggest mappings for one-time migration; never persists them."""
        db: AsyncSession = info.context["db"]
        resolved_table_id = _require_task_profile_db(table_id)
        await _require_item_permission(info, resolved_table_id, "read")
        try:
            return await suggest_task_profile(db, resolved_table_id)
        except TaskProfileValidationError as exc:
            raise GraphQLError(str(exc)) from exc
