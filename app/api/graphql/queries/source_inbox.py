from __future__ import annotations

from typing import Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.graphql.helpers import _require_user, _resolve_backend
from app.services.source_inbox import SourceInboxError, source_inbox_service


@strawberry.type
class SourceInboxQueries:
    @strawberry.field(name="sourceInboxItems")
    async def source_inbox_items(
        self,
        info: Info,
        workspace_id: str,
        status: Optional[str] = None,
        search: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Source inbox requires database backend")
        user = await _require_user(info)
        try:
            return await source_inbox_service.list_items(
                info.context["db"],
                user_id=user.id,
                workspace_id=workspace_id,
                status=status,
                search=search,
                offset=offset,
                limit=limit,
            )
        except (SourceInboxError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc

    @strawberry.field(name="sourceInboxItem")
    async def source_inbox_item(
        self,
        info: Info,
        item_id: str,
    ) -> JSON:
        if _resolve_backend(None) != "db":
            raise GraphQLError("Source inbox requires database backend")
        user = await _require_user(info)
        try:
            return await source_inbox_service.get_item(
                info.context["db"], user_id=user.id, item_id=item_id
            )
        except (SourceInboxError, PermissionError, ValueError) as exc:
            raise GraphQLError(str(exc)) from exc
