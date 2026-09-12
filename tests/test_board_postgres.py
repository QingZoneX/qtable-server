from __future__ import annotations

import pytest

from app.db.session import AsyncSessionLocal, engine
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    TableView,
    WorkspaceItem,
)
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.board import move_board_card, query_board


pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL-specific Kanban integration contract",
)


@pytest.mark.asyncio
async def test_board_postgresql_json_permission_paging_and_fractional_rank():
    user_id = 947001
    hidden_user_id = 947002
    workspace_id = "ws-board-pg"
    table_id = "tbl-board-pg"
    view_id = "view-board-pg"

    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                User(
                    id=user_id,
                    email="board-pg@example.test",
                    name="Board PG",
                    password_hash="!test",
                ),
                User(
                    id=hidden_user_id,
                    email="board-pg-hidden@example.test",
                    name="Board PG Hidden",
                    password_hash="!test",
                ),
                Workspace(id=workspace_id, name="Board PostgreSQL"),
                WorkspaceItem(
                    id="root-board-pg",
                    workspace_id=workspace_id,
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id=table_id,
                    workspace_id=workspace_id,
                    type="table",
                    name="Board Tasks",
                    parent_id="root-board-pg",
                    order_index=1,
                    default_view_id=view_id,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=hidden_user_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                TableField(
                    id="title",
                    table_id=table_id,
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id=table_id,
                    name="Status",
                    type="select",
                    options=[
                        {"id": "todo", "label": "Todo"},
                        {"id": "doing", "label": "Doing"},
                    ],
                    order_index=1,
                ),
                TableField(
                    id="owner",
                    table_id=table_id,
                    name="Owner",
                    type="member",
                    property={"multiple": False},
                    order_index=2,
                ),
                TableView(
                    id=view_id,
                    table_id=table_id,
                    name="Kanban",
                    type="board",
                    config={
                        "groupConfig": {"fieldId": "status", "order": "asc"},
                        "boardConfig": {
                            "groupFieldId": "status",
                            "laneFieldId": "owner",
                            "cardFieldIds": ["title"],
                            "cardOrder": "manual",
                        },
                    },
                ),
                TableRowPermissionPolicy(
                    table_id=table_id,
                    mode="member_field",
                    member_field_id="owner",
                ),
                TableRecord(
                    id="pg-a",
                    table_id=table_id,
                    data={"title": "A", "status": "todo", "owner": str(user_id)},
                    order_index=0,
                    created_by_user_id=hidden_user_id,
                    version=1,
                ),
                TableRecord(
                    id="pg-b",
                    table_id=table_id,
                    data={"title": "B", "status": "todo", "owner": str(user_id)},
                    order_index=1,
                    created_by_user_id=hidden_user_id,
                    version=1,
                ),
                TableRecord(
                    id="pg-c",
                    table_id=table_id,
                    data={"title": "C", "status": "todo", "owner": str(hidden_user_id)},
                    order_index=2,
                    created_by_user_id=hidden_user_id,
                    version=1,
                ),
            ]
        )
        await session.commit()

        policy = {
            "mode": "member_field",
            "memberFieldId": "owner",
            "enabled": True,
        }
        initial = await query_board(
            session,
            table_id=table_id,
            view_id=view_id,
            user_id=user_id,
            table_permission="edit",
            row_policy=policy,
            column_key="select:todo",
            lane_key=f"member:{user_id}",
            limit=1,
        )
        assert initial["pageInfo"]["databasePaged"] is True
        assert initial["pageInfo"]["totalCount"] == 2
        assert len(initial["cards"]) == 1
        assert initial["pageInfo"]["hasMore"] is True

        moved = await move_board_card(
            session,
            table_id=table_id,
            view_id=view_id,
            record_id="pg-b",
            target_column_key="select:todo",
            target_lane_key=f"member:{user_id}",
            after_record_id="pg-a",
            expected_record_version=1,
            expected_order_revision=0,
            user_id=user_id,
        )
        assert moved["orderRevision"] == 1

        reordered = await query_board(
            session,
            table_id=table_id,
            view_id=view_id,
            user_id=user_id,
            table_permission="edit",
            row_policy=policy,
            column_key="select:todo",
            lane_key=f"member:{user_id}",
            limit=10,
        )
        assert [card["record"]["id"] for card in reordered["cards"]] == [
            "pg-b",
            "pg-a",
        ]
        assert "pg-c" not in {
            card["record"]["id"] for card in reordered["cards"]
        }
