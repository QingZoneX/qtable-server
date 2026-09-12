from __future__ import annotations

from typing import List, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _require_item_permission,
    _require_record_permission,
    _require_user,
    _resolve_backend,
    _resolve_db_table_id,
    publish_table_update,
)
from app.services.board import (
    BoardError,
    move_board_card,
    publish_board_update,
    update_board_view_config,
)


@strawberry.type
class BoardMutations:
    @strawberry.mutation(name="updateBoardViewConfig")
    async def update_board_view_config_mutation(
        self,
        info: Info,
        table_id: str,
        view_id: str,
        group_field_id: str,
        lane_field_id: Optional[str] = None,
        card_field_ids: Optional[List[str]] = None,
        hide_completed: bool = False,
        collapsed_columns: Optional[List[str]] = None,
    ) -> JSON:
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Kanban configuration requires database backend")
        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        await _require_item_permission(info, resolved_table_id, "edit")
        try:
            result = await update_board_view_config(
                db,
                table_id=resolved_table_id,
                view_id=view_id,
                group_field_id=group_field_id,
                lane_field_id=lane_field_id,
                card_field_ids=card_field_ids,
                hide_completed=hide_completed,
                collapsed_columns=collapsed_columns,
            )
        except BoardError as exc:
            raise GraphQLError(str(exc)) from exc

        await publish_board_update(
            {
                "type": "config",
                "tableId": resolved_table_id,
                "viewId": view_id,
            }
        )
        await publish_table_update(db, resolved_table_id)
        return result

    @strawberry.mutation(name="moveBoardCard")
    async def move_board_card_mutation(
        self,
        info: Info,
        table_id: str,
        view_id: str,
        record_id: strawberry.ID,
        target_column_key: str,
        target_lane_key: Optional[str] = None,
        before_record_id: Optional[strawberry.ID] = None,
        after_record_id: Optional[strawberry.ID] = None,
        expected_record_version: Optional[int] = None,
        expected_order_revision: Optional[int] = None,
    ) -> JSON:
        """Atomically update business state and per-view manual card order."""
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Kanban moves require database backend")

        db: AsyncSession = info.context["db"]
        resolved_table_id = _resolve_db_table_id(table_id)
        normalized_record_id = str(record_id)
        await _require_record_permission(
            info,
            resolved_table_id,
            normalized_record_id,
            "update",
        )
        for anchor in (before_record_id, after_record_id):
            if anchor is not None:
                await _require_record_permission(
                    info,
                    resolved_table_id,
                    str(anchor),
                    "read",
                )
        user = await _require_user(info)

        try:
            result = await move_board_card(
                db,
                table_id=resolved_table_id,
                view_id=view_id,
                record_id=normalized_record_id,
                target_column_key=target_column_key,
                target_lane_key=target_lane_key,
                before_record_id=str(before_record_id) if before_record_id is not None else None,
                after_record_id=str(after_record_id) if after_record_id is not None else None,
                expected_record_version=expected_record_version,
                expected_order_revision=expected_order_revision,
                user_id=user.id,
            )
        except BoardError as exc:
            raise GraphQLError(str(exc)) from exc

        await publish_board_update(
            {
                "type": "card_moved",
                "tableId": resolved_table_id,
                "viewId": view_id,
                "recordId": normalized_record_id,
                "previousColumnKey": result.get("previousColumnKey"),
                "previousLaneKey": result.get("previousLaneKey"),
                "columnKey": result.get("columnKey"),
                "laneKey": result.get("laneKey"),
                "orderRevision": result.get("orderRevision"),
                "recordVersion": result.get("recordVersion"),
            }
        )
        await publish_table_update(db, resolved_table_id)
        return result
