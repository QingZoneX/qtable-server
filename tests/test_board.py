from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.core.config import settings
from app.db.base import Base
from app.models.board import BoardCardOrder
from app.models.change_history import ChangeSet
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    TableView,
    WorkspaceItem,
)
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.board import (
    BoardConfigurationError,
    BoardConflictError,
    move_board_card,
    query_board,
    update_board_view_config,
)
from app.services.smart_table_store.db_backend import delete_record, delete_view


ALICE = 14711
BOB = 14712
TABLE_ID = "tbl-board-test"
VIEW_ID = "view-board"


@pytest_asyncio.fixture
async def board_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'board.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                User(
                    id=ALICE,
                    email="alice-board@example.test",
                    name="Alice",
                    password_hash="!test",
                ),
                User(
                    id=BOB,
                    email="bob-board@example.test",
                    name="Bob",
                    password_hash="!test",
                ),
                Workspace(id="ws-board-test", name="Board Test"),
                WorkspaceItem(
                    id="root-board-test",
                    workspace_id="ws-board-test",
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id=TABLE_ID,
                    workspace_id="ws-board-test",
                    type="table",
                    name="Tasks",
                    parent_id="root-board-test",
                    order_index=1,
                    default_view_id=VIEW_ID,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=ALICE,
                    workspace_id="ws-board-test",
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=BOB,
                    workspace_id="ws-board-test",
                    role=WorkspaceRole.editor,
                ),
                TableField(
                    id="title",
                    table_id=TABLE_ID,
                    name="任务名称",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id=TABLE_ID,
                    name="状态",
                    type="select",
                    options=[
                        {"id": "todo", "label": "待开始"},
                        {"id": "doing", "label": "进行中"},
                        {"id": "done", "label": "已完成"},
                    ],
                    order_index=1,
                ),
                TableField(
                    id="owner",
                    table_id=TABLE_ID,
                    name="负责人",
                    type="member",
                    property={"multiple": False},
                    order_index=2,
                ),
                TableField(
                    id="owner_multi",
                    table_id=TABLE_ID,
                    name="协作人",
                    type="member",
                    property={"multiple": True},
                    order_index=3,
                ),
                TableView(
                    id=VIEW_ID,
                    table_id=TABLE_ID,
                    name="Kanban",
                    type="board",
                    config={
                        "groupConfig": {"fieldId": "status", "order": "asc"},
                        "boardConfig": {
                            "groupFieldId": "status",
                            "laneFieldId": "owner",
                            "cardFieldIds": ["title"],
                            "cardOrder": "manual",
                            "hideCompleted": False,
                            "collapsedColumns": [],
                        },
                    },
                ),
                TableView(
                    id="view-grid",
                    table_id=TABLE_ID,
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableRowPermissionPolicy(
                    table_id=TABLE_ID,
                    mode="member_field",
                    member_field_id="owner",
                ),
            ]
        )
        session.add_all(
            [
                TableRecord(
                    id="r1",
                    table_id=TABLE_ID,
                    data={"title": "First", "status": "todo", "owner": str(ALICE)},
                    order_index=0,
                    created_by_user_id=BOB,
                    version=1,
                ),
                TableRecord(
                    id="r2",
                    table_id=TABLE_ID,
                    data={"title": "Second", "status": "todo", "owner": str(ALICE)},
                    order_index=1,
                    created_by_user_id=BOB,
                    version=1,
                ),
                TableRecord(
                    id="r3",
                    table_id=TABLE_ID,
                    data={"title": "Third", "status": "doing", "owner": str(ALICE)},
                    order_index=2,
                    created_by_user_id=BOB,
                    version=1,
                ),
                TableRecord(
                    id="hidden",
                    table_id=TABLE_ID,
                    data={"title": "Hidden", "status": "todo", "owner": str(BOB)},
                    order_index=3,
                    created_by_user_id=BOB,
                    version=1,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


def _policy():
    return {"mode": "member_field", "memberFieldId": "owner", "enabled": True}


@pytest.mark.asyncio
async def test_board_query_counts_and_cards_are_row_permission_safe(board_db):
    payload = await query_board(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        user_id=ALICE,
        table_permission="edit",
        row_policy=_policy(),
        column_key="select:todo",
        lane_key=f"member:{ALICE}",
        limit=20,
    )

    columns = {item["key"]: item for item in payload["columns"]}
    assert columns["select:todo"]["count"] == 2
    assert columns["select:doing"]["count"] == 1
    assert {card["record"]["id"] for card in payload["cards"]} == {"r1", "r2"}
    assert payload["pageInfo"]["totalCount"] == 2
    assert payload["pageInfo"]["databasePaged"] is True
    assert all(cell["count"] <= 3 for cell in payload["cells"])
    assert "hidden" not in {card["record"]["id"] for card in payload["cards"]}


@pytest.mark.asyncio
async def test_same_column_move_persists_manual_order_and_detects_stale_revision(board_db):
    moved = await move_board_card(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        record_id="r2",
        target_column_key="select:todo",
        target_lane_key=f"member:{ALICE}",
        after_record_id="r1",
        expected_record_version=1,
        expected_order_revision=0,
        user_id=ALICE,
    )
    assert moved["recordVersion"] == 1
    assert moved["orderRevision"] == 1
    assert Decimal(moved["rank"]) < Decimal(0)

    payload = await query_board(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        user_id=ALICE,
        table_permission="edit",
        row_policy=_policy(),
        column_key="select:todo",
        lane_key=f"member:{ALICE}",
        limit=20,
    )
    assert [card["record"]["id"] for card in payload["cards"]] == ["r2", "r1"]

    with pytest.raises(BoardConflictError, match="Card order changed"):
        await move_board_card(
            board_db,
            table_id=TABLE_ID,
            view_id=VIEW_ID,
            record_id="r2",
            target_column_key="select:todo",
            target_lane_key=f"member:{ALICE}",
            before_record_id="r1",
            expected_record_version=1,
            expected_order_revision=0,
            user_id=ALICE,
        )


@pytest.mark.asyncio
async def test_cross_column_move_updates_status_and_rank_in_one_committed_change(board_db):
    moved = await move_board_card(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        record_id="r1",
        target_column_key="select:doing",
        target_lane_key=f"member:{ALICE}",
        before_record_id="r3",
        expected_record_version=1,
        expected_order_revision=0,
        user_id=ALICE,
    )
    assert moved["record"]["status"] == "doing"
    assert moved["recordVersion"] == 2
    assert moved["orderRevision"] == 1

    row = (
        await board_db.execute(
            select(TableRecord).where(TableRecord.table_id == TABLE_ID, TableRecord.id == "r1")
        )
    ).scalars().one()
    order = (
        await board_db.execute(
            select(BoardCardOrder).where(
                BoardCardOrder.table_id == TABLE_ID,
                BoardCardOrder.view_id == VIEW_ID,
                BoardCardOrder.record_id == "r1",
            )
        )
    ).scalars().one()
    history = (
        await board_db.execute(
            select(ChangeSet).where(ChangeSet.table_id == TABLE_ID).order_by(ChangeSet.created_at.desc())
        )
    ).scalars().first()
    assert row.data["status"] == "doing"
    assert row.version == 2
    assert order.revision == 1
    assert history is not None and history.source == "kanban"


@pytest.mark.asyncio
async def test_invalid_anchor_rolls_back_without_changing_business_state(board_db):
    with pytest.raises(BoardConflictError, match="target column"):
        await move_board_card(
            board_db,
            table_id=TABLE_ID,
            view_id=VIEW_ID,
            record_id="r1",
            target_column_key="select:doing",
            target_lane_key=f"member:{ALICE}",
            after_record_id="r2",
            expected_record_version=1,
            user_id=ALICE,
        )

    row = (
        await board_db.execute(
            select(TableRecord).where(TableRecord.table_id == TABLE_ID, TableRecord.id == "r1")
        )
    ).scalars().one()
    assert row.data["status"] == "todo"
    assert row.version == 1
    assert (
        await board_db.execute(
            select(BoardCardOrder).where(
                BoardCardOrder.table_id == TABLE_ID,
                BoardCardOrder.view_id == VIEW_ID,
                BoardCardOrder.record_id == "r1",
            )
        )
    ).scalars().first() is None


@pytest.mark.asyncio
async def test_member_swimlane_requires_single_select_and_config_persists(board_db):
    with pytest.raises(BoardConfigurationError, match="single-select"):
        await update_board_view_config(
            board_db,
            table_id=TABLE_ID,
            view_id=VIEW_ID,
            group_field_id="status",
            lane_field_id="owner_multi",
        )

    result = await update_board_view_config(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        group_field_id="status",
        lane_field_id="owner",
        card_field_ids=["title", "status"],
        hide_completed=True,
        collapsed_columns=["select:done"],
    )
    assert result["boardConfig"]["groupFieldId"] == "status"
    assert result["boardConfig"]["laneFieldId"] == "owner"
    assert result["boardConfig"]["cardFieldIds"] == ["title", "status"]
    assert result["boardConfig"]["hideCompleted"] is True


@pytest.mark.asyncio
async def test_board_cursor_pages_each_cell_without_loading_entire_table(board_db):
    board_db.add_all(
        [
            TableRecord(
                id=f"bulk-{index:03d}",
                table_id=TABLE_ID,
                data={"title": f"Bulk {index}", "status": "todo", "owner": str(ALICE)},
                order_index=10 + index,
                created_by_user_id=BOB,
                version=1,
            )
            for index in range(120)
        ]
    )
    await board_db.commit()

    first = await query_board(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        user_id=ALICE,
        table_permission="edit",
        row_policy=_policy(),
        column_key="select:todo",
        lane_key=f"member:{ALICE}",
        limit=25,
    )
    assert len(first["cards"]) == 25
    assert first["pageInfo"]["totalCount"] == 122
    assert first["pageInfo"]["hasMore"] is True
    assert first["pageInfo"]["databasePaged"] is True

    second = await query_board(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        user_id=ALICE,
        table_permission="edit",
        row_policy=_policy(),
        column_key="select:todo",
        lane_key=f"member:{ALICE}",
        cursor=first["pageInfo"]["nextCursor"],
        limit=25,
    )
    first_ids = {card["record"]["id"] for card in first["cards"]}
    second_ids = {card["record"]["id"] for card in second["cards"]}
    assert len(second["cards"]) == 25
    assert first_ids.isdisjoint(second_ids)


@pytest.mark.asyncio
async def test_record_and_view_deletion_clean_board_order_rows(board_db):
    await move_board_card(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        record_id="r2",
        target_column_key="select:todo",
        target_lane_key=f"member:{ALICE}",
        after_record_id="r1",
        user_id=ALICE,
    )
    assert (
        await board_db.execute(
            select(BoardCardOrder).where(BoardCardOrder.record_id == "r2")
        )
    ).scalars().first() is not None

    assert await delete_record(board_db, TABLE_ID, "r2", user_id=ALICE)
    assert (
        await board_db.execute(
            select(BoardCardOrder).where(BoardCardOrder.record_id == "r2")
        )
    ).scalars().first() is None

    await move_board_card(
        board_db,
        table_id=TABLE_ID,
        view_id=VIEW_ID,
        record_id="r1",
        target_column_key="select:todo",
        target_lane_key=f"member:{ALICE}",
        user_id=ALICE,
    )
    assert await delete_view(board_db, TABLE_ID, VIEW_ID)
    assert (
        await board_db.execute(
            select(BoardCardOrder).where(BoardCardOrder.view_id == VIEW_ID)
        )
    ).scalars().first() is None


def test_graphql_schema_exposes_board_contract():
    rendered = str(schema.as_str())
    assert "boardView" in rendered
    assert "moveBoardCard" in rendered
    assert "updateBoardViewConfig" in rendered
    assert "boardUpdates" in rendered
