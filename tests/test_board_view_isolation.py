from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.board import BoardCardOrder
from app.models.smart_table import TableField, TableRecord, TableView
from app.services.board import query_board


TABLE_ID = "tbl-board-view-isolation"
VIEW_A = "board-a"
VIEW_B = "board-b"
USER_ID = 7001


def _board_config() -> dict:
    return {
        "groupConfig": {"fieldId": "status", "order": "asc"},
        "boardConfig": {
            "groupFieldId": "status",
            "laneFieldId": None,
            "cardFieldIds": ["title"],
            "cardOrder": "manual",
            "hideCompleted": False,
            "collapsedColumns": [],
        },
    }


@pytest_asyncio.fixture
async def isolation_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'board-view-isolation.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                TableField(
                    id="title",
                    table_id=TABLE_ID,
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id=TABLE_ID,
                    name="Status",
                    type="select",
                    options=[{"id": "todo", "label": "Todo"}],
                    order_index=1,
                ),
                TableField(
                    id="owner",
                    table_id=TABLE_ID,
                    name="Owner",
                    type="member",
                    property={"multiple": False},
                    order_index=2,
                ),
                TableView(
                    id=VIEW_A,
                    table_id=TABLE_ID,
                    name="Board A",
                    type="board",
                    config=_board_config(),
                ),
                TableView(
                    id=VIEW_B,
                    table_id=TABLE_ID,
                    name="Board B",
                    type="board",
                    config=_board_config(),
                ),
                TableRecord(
                    id="r1",
                    table_id=TABLE_ID,
                    data={"title": "First", "status": "todo", "owner": str(USER_ID)},
                    order_index=0,
                    version=7,
                ),
                TableRecord(
                    id="r2",
                    table_id=TABLE_ID,
                    data={"title": "Second", "status": "todo", "owner": str(USER_ID)},
                    order_index=1,
                    version=9,
                ),
                # The two views intentionally persist opposite manual orders.
                # Fractional values also prove that the compatibility path does
                # not quantize ranks back to whole RANK_STEP buckets.
                BoardCardOrder(
                    table_id=TABLE_ID,
                    view_id=VIEW_A,
                    record_id="r1",
                    rank=Decimal("1000000.25"),
                    revision=3,
                ),
                BoardCardOrder(
                    table_id=TABLE_ID,
                    view_id=VIEW_A,
                    record_id="r2",
                    rank=Decimal("1000000.75"),
                    revision=4,
                ),
                BoardCardOrder(
                    table_id=TABLE_ID,
                    view_id=VIEW_B,
                    record_id="r1",
                    rank=Decimal("2000000.75"),
                    revision=8,
                ),
                BoardCardOrder(
                    table_id=TABLE_ID,
                    view_id=VIEW_B,
                    record_id="r2",
                    rank=Decimal("2000000.25"),
                    revision=9,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


async def _query(session, view_id: str):
    # Member filtering is intentionally handled by the compatibility path; it
    # is not one of record_query's exact SQL filter types.
    return await query_board(
        session,
        table_id=TABLE_ID,
        view_id=view_id,
        user_id=USER_ID,
        table_permission="edit",
        row_policy={"mode": "all", "memberFieldId": None, "enabled": False},
        column_key="select:todo",
        limit=20,
        filters=[{"fieldId": "owner", "operator": "equals", "value": str(USER_ID)}],
    )


@pytest.mark.asyncio
async def test_compatibility_manual_order_is_isolated_per_view_and_preserves_versions(
    isolation_db,
):
    board_a = await _query(isolation_db, VIEW_A)
    board_b = await _query(isolation_db, VIEW_B)

    assert board_a["pageInfo"]["databasePaged"] is False
    assert board_a["pageInfo"]["manualOrderActive"] is True
    assert [card["record"]["id"] for card in board_a["cards"]] == ["r1", "r2"]
    assert [card["record"]["id"] for card in board_b["cards"]] == ["r2", "r1"]

    a_by_id = {card["record"]["id"]: card for card in board_a["cards"]}
    b_by_id = {card["record"]["id"]: card for card in board_b["cards"]}

    assert a_by_id["r1"]["orderRevision"] == 3
    assert a_by_id["r2"]["orderRevision"] == 4
    assert b_by_id["r1"]["orderRevision"] == 8
    assert b_by_id["r2"]["orderRevision"] == 9
    assert a_by_id["r1"]["recordVersion"] == 7
    assert a_by_id["r2"]["recordVersion"] == 9

    assert Decimal(a_by_id["r1"]["rank"]) < Decimal(a_by_id["r2"]["rank"])
    assert Decimal(b_by_id["r2"]["rank"]) < Decimal(b_by_id["r1"]["rank"])
    assert Decimal(a_by_id["r1"]["rank"]) != Decimal("1000000")


@pytest.mark.asyncio
async def test_compatibility_sort_never_leaks_internal_order_marker(isolation_db):
    payload = await query_board(
        isolation_db,
        table_id=TABLE_ID,
        view_id=VIEW_A,
        user_id=USER_ID,
        table_permission="edit",
        row_policy={"mode": "all", "memberFieldId": None, "enabled": False},
        column_key="select:todo",
        limit=20,
        sorts=[{"fieldId": "owner", "order": "asc"}],
    )

    assert payload["pageInfo"]["databasePaged"] is False
    assert payload["pageInfo"]["manualOrderActive"] is False
    assert all("__boardFallbackOrder" not in card["record"] for card in payload["cards"])
    assert {card["recordVersion"] for card in payload["cards"]} == {7, 9}
