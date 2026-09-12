from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.workspace_member import Workspace
from app.services.smart_table_store import (
    apply_record_patches,
    create_records_with_data,
    update_record,
    update_record_fields,
)


@pytest.fixture
async def text_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'text-normalization-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(Workspace(id="ws-text", name="Text Workspace"))
        session.add(
            WorkspaceItem(
                id="text-table",
                workspace_id="ws-text",
                type="table",
                name="Text Table",
                parent_id=None,
                order_index=0,
            )
        )
        session.add_all(
            [
                TableField(
                    id="title",
                    table_id="text-table",
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="note",
                    table_id="text-table",
                    name="Note",
                    type="text",
                    order_index=1,
                ),
                TableField(
                    id="count",
                    table_id="text-table",
                    name="Count",
                    type="number",
                    order_index=2,
                ),
                TableRecord(
                    id="r1",
                    table_id="text-table",
                    data={"title": "Initial", "note": "", "count": 1},
                    order_index=0,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_single_text_update_trims_spaces_tabs_and_edge_blank_lines(text_db):
    updated = await update_record(
        text_db,
        "text-table",
        "r1",
        "title",
        "\n\t   Hello world   \n\n",
    )
    assert updated is not None
    assert updated["title"] == "Hello world"


@pytest.mark.asyncio
async def test_text_normalization_preserves_internal_whitespace(text_db):
    updated = await update_record(
        text_db,
        "text-table",
        "r1",
        "note",
        "\n  first line\n\nsecond line  \n",
    )
    assert updated is not None
    assert updated["note"] == "first line\n\nsecond line"


@pytest.mark.asyncio
async def test_multi_field_and_patch_writes_normalize_text_only(text_db):
    updated = await update_record_fields(
        text_db,
        "text-table",
        "r1",
        {"title": "  Updated  ", "count": 7},
    )
    assert updated is not None
    assert updated["title"] == "Updated"
    assert updated["count"] == 7

    await apply_record_patches(
        text_db,
        "text-table",
        [
            {
                "recordId": "r1",
                "fieldId": "note",
                "value": "\n  patched note \n",
            }
        ],
    )
    record = await text_db.get(TableRecord, ("r1", "text-table"))
    assert record is not None
    assert record.data["note"] == "patched note"


@pytest.mark.asyncio
async def test_batch_create_normalizes_text_values(text_db):
    created = await create_records_with_data(
        text_db,
        "text-table",
        [
            {
                "title": "  Imported title  ",
                "note": "\nImported note\n",
                "count": 3,
            }
        ],
    )
    assert len(created) == 1
    assert created[0]["title"] == "Imported title"
    assert created[0]["note"] == "Imported note"
    assert created[0]["count"] == 3
