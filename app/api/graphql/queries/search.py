from __future__ import annotations

from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.global_search import (
    DEFAULT_GLOBAL_SEARCH_LIMIT,
    global_search,
)


@strawberry.type
class SearchQueries:
    @strawberry.field(name="globalSearch")
    async def global_search_query(
        self,
        info: Info,
        keyword: str,
        workspace_id: Optional[str] = None,
        entity_types: Optional[List[str]] = None,
        cursor: Optional[str] = None,
        limit: int = DEFAULT_GLOBAL_SEARCH_LIMIT,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Global search requires database backend")

        db: AsyncSession = info.context["db"]
        user = await _require_user(info)
        try:
            return await global_search(
                db,
                user_id=user.id,
                keyword=keyword,
                workspace_id=workspace_id,
                entity_types=entity_types,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise GraphQLError(str(exc)) from exc
