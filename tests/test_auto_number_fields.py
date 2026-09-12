from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableRecord, WorkspaceItem
from app.models.workspace_member import Workspace
from app.services.auto_number_engine import (
    AutoNumberValidationError,
    format_auto_number,
)
from app.services.smart_table_store import (
    add_field,
    create_record,
    create_records_with_data,
    delete_record,
    update_field,
    update_record,
)


@pytest.fixture
async def auto_number_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'auto-number-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(Workspace(id="ws-auto", name="Auto Workspace"))
        session.add(
            WorkspaceItem(
                id="auto-table",
                workspace_id="ws-auto",
                type="table",
                name="Auto Table",
                parent_id=None,
                order_index=0,
            )
        )
        session.add_all(
            [
                TableRecord(
                    id="r1",
                    table_id="auto-table",
                    data={},
                    order_index=0,
                ),
                TableRecord(
                    id="r2",
                    table_id="auto-table",
                    data={},
                    order_index=1,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


def auto_field(**property_overrides):
    return {
        "id": "ticket-no",
        "name": "Ticket No.",
        "type": "autoNumber",
        "property": {
            "prefix": "TASK-",
            "digits": 3,
            "start": 1,
            **property_overrides,
        },
    }


@pytest.mark.asyncio
async def test_add_auto_number_field_backfills_existing_rows(auto_number_db):
    created = await add_field(auto_number_db, "auto-table", auto_field())

    assert created["property"]["nextNumber"] == 3
    first = await auto_number_db.get(TableRecord, ("r1", "auto-table"))
    second = await auto_number_db.get(TableRecord, ("r2", "auto-table"))
    assert first is not None and first.data["ticket-no"] == 1
    assert second is not None and second.data["ticket-no"] == 2


@pytest.mark.asyncio
async def test_new_rows_continue_sequence_without_reusing_deleted_numbers(auto_number_db):
    await add_field(auto_number_db, "auto-table", auto_field())

    third = await create_record(auto_number_db, "auto-table")
    assert third["ticket-no"] == 3

    assert await delete_record(auto_number_db, "auto-table", "r2") is True
    fourth = await create_record(auto_number_db, "auto-table")
    assert fourth["ticket-no"] == 4


@pytest.mark.asyncio
async def test_batch_import_cannot_override_auto_number(auto_number_db):
    await add_field(auto_number_db, "auto-table", auto_field(start=10))

    created = await create_records_with_data(
        auto_number_db,
        "auto-table",
        [{"ticket-no": 999999}, {"ticket-no": -5}],
    )
    assert [row["ticket-no"] for row in created] == [12, 13]


@pytest.mark.asyncio
async def test_auto_number_is_read_only(auto_number_db):
    await add_field(auto_number_db, "auto-table", auto_field())

    with pytest.raises(AutoNumberValidationError, match="read-only"):
        await update_record(
            auto_number_db,
            "auto-table",
            "r1",
            "ticket-no",
            999,
        )


@pytest.mark.asyncio
async def test_format_changes_preserve_sequence_counter(auto_number_db):
    await add_field(auto_number_db, "auto-table", auto_field())
    await create_record(auto_number_db, "auto-table")

    updated = await update_field(
        auto_number_db,
        "auto-table",
        "ticket-no",
        {
            "property": {
                "prefix": "ISSUE-",
                "digits": 5,
                "start": 1,
            }
        },
    )
    assert updated is not None
    assert updated["property"]["nextNumber"] == 4
    assert format_auto_number(updated, 1) == "ISSUE-00001"

    next_row = await create_record(auto_number_db, "auto-table")
    assert next_row["ticket-no"] == 4


@pytest.mark.asyncio
async def test_auto_number_config_validation(auto_number_db):
    with pytest.raises(AutoNumberValidationError, match="digits"):
        await add_field(
            auto_number_db,
            "auto-table",
            auto_field(digits=0),
        )

    with pytest.raises(AutoNumberValidationError, match="prefix"):
        await add_field(
            auto_number_db,
            "auto-table",
            auto_field(prefix="X" * 65),
        )


@pytest.mark.asyncio
async def test_client_cannot_seed_or_move_server_counter(auto_number_db):
    seeded = auto_field(nextNumber=900)
    created_field = await add_field(auto_number_db, "auto-table", seeded)
    # Two existing rows consume 1 and 2; the untrusted nextNumber=900 is ignored.
    assert created_field["property"]["nextNumber"] == 3

    updated = await update_field(
        auto_number_db,
        "auto-table",
        "ticket-no",
        {
            "property": {
                "prefix": "SAFE-",
                "digits": 4,
                "start": 500,
                "nextNumber": 9999,
            }
        },
    )
    assert updated is not None
    assert updated["property"]["start"] == 1
    assert updated["property"]["nextNumber"] == 3

    row = await create_record(auto_number_db, "auto-table")
    assert row["ticket-no"] == 3
