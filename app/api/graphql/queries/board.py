from __future__ import annotations

from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _resolve_backend,
    _resolve_db_table_id,
    _row_permission_context,
)
from app.services.board import BoardError, query_board


@strawberry.type
class BoardQueries:
    @strawberry.field(name="boardView")
    async def board_view(
        self,
        info: Info,
        table_id: str,
        view_id: str,
        column_key: Optional[str] = None,
        lane_key: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
        filters: Optional[List[JSON]] = None,
        sorts: Optional[List[JSON]] = None,
    ) -> JSON:
        """Return permission-filtered Kanban metadata and one independently paged cell.

        Calling without ``columnKey`` is a cheap metadata/count request. A
        column (and optional swimlane) can then be paged independently with the
        opaque cursor returned by ``pageInfo.nextCursor``.
        """
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Kanban paging requires database backend")

        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        user, table_permission, row_policy = await _row_permission_context(
            info,
            resolved_table_id,
            "read",
        )
        try:
            return await query_board(
                db,
                table_id=resolved_table_id,
                view_id=view_id,
                user_id=user.id,
                table_permission=table_permission,
                row_policy=row_policy,
                column_key=column_key,
                lane_key=lane_key,
                cursor=cursor,
                limit=limit,
                filters=filters or [],
                sorts=sorts or [],
            )
        except BoardError as exc:
            raise GraphQLError(str(exc)) from exc
