from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, TableView, WorkspaceItem
from app.models.workspace_member import Workspace
from app.services.smart_table_store import create_record, get_full_store, init_table_db
from app.services.workspace.db_ops import create_table_db


@pytest.fixture
async def init_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'table-init-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(Workspace(id="ws-init", name="Init Workspace"))
        session.add(
            WorkspaceItem(
                id="root-init",
                workspace_id="ws-init",
                type="folder",
                name="Root",
                parent_id=None,
                order_index=0,
                default_view_id=None,
            )
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_blank_table_creation_has_default_text_field_and_grid_view(init_db):
    created = await create_table_db(
        init_db,
        "Blank",
        "root-init",
        "v1",
        "ws-init",
        "blank",
    )
    table_id = created["id"]

    fields = (
        await init_db.execute(
            select(TableField)
            .where(TableField.table_id == table_id)
            .order_by(TableField.order_index)
        )
    ).scalars().all()
    views = (
        await init_db.execute(
            select(TableView).where(TableView.table_id == table_id)
        )
    ).scalars().all()

    assert len(fields) == 1
    assert fields[0].id == "f1"
    assert fields[0].type == "text"
    assert fields[0].name == "文本"
    assert len(views) == 1
    assert views[0].id == "v1"
    assert views[0].type == "grid"


@pytest.mark.asyncio
async def test_partial_table_repair_restores_fields_without_duplicate_view(init_db):
    init_db.add(
        WorkspaceItem(
            id="broken-table",
            workspace_id="ws-init",
            type="table",
            name="Broken",
            parent_id="root-init",
            order_index=1,
            default_view_id="v1",
        )
    )
    init_db.add(
        TableView(
            id="v1",
            table_id="broken-table",
            name="表格视图",
            type="grid",
            config={"filters": [], "sorts": []},
        )
    )
    init_db.add(
        TableRecord(
            id="r-existing",
            table_id="broken-table",
            data={},
            order_index=0,
        )
    )
    await init_db.commit()

    changed = await init_table_db(init_db, "broken-table", "blank")
    assert changed is True

    store = await get_full_store(init_db, "broken-table")
    assert [field["id"] for field in store["fields"]] == ["f1"]
    assert [view["id"] for view in store["views"]] == ["v1"]
    assert store["records"][0]["id"] == "r-existing"
    assert store["records"][0]["f1"] is None

    # Repair is idempotent: a second pass must not duplicate either object.
    changed_again = await init_table_db(init_db, "broken-table", "blank")
    assert changed_again is False
    store_again = await get_full_store(init_db, "broken-table")
    assert len(store_again["fields"]) == 1
    assert len(store_again["views"]) == 1


@pytest.mark.asyncio
async def test_partial_table_repair_restores_missing_view_without_replacing_fields(init_db):
    init_db.add(
        WorkspaceItem(
            id="missing-view",
            workspace_id="ws-init",
            type="table",
            name="Missing View",
            parent_id="root-init",
            order_index=2,
            default_view_id="v1",
        )
    )
    init_db.add(
        TableField(
            id="custom",
            table_id="missing-view",
            name="Custom",
            type="text",
            order_index=0,
        )
    )
    await init_db.commit()

    await init_table_db(init_db, "missing-view", "blank")
    store = await get_full_store(init_db, "missing-view")

    assert [field["id"] for field in store["fields"]] == ["custom"]
    assert [view["id"] for view in store["views"]] == ["v1"]


@pytest.mark.asyncio
async def test_insert_into_legacy_zero_field_table_repairs_before_creating_row(init_db):
    init_db.add(
        WorkspaceItem(
            id="zero-field",
            workspace_id="ws-init",
            type="table",
            name="Zero Field",
            parent_id="root-init",
            order_index=3,
            default_view_id="v1",
        )
    )
    init_db.add(
        TableView(
            id="v1",
            table_id="zero-field",
            name="表格视图",
            type="grid",
            config={},
        )
    )
    await init_db.commit()

    created = await create_record(init_db, "zero-field")
    assert "f1" in created
    assert created["f1"] is None

    store = await get_full_store(init_db, "zero-field")
    assert len(store["fields"]) == 1
    assert len(store["records"]) == 1


@pytest.mark.asyncio
async def test_create_table_rolls_back_workspace_shell_when_init_fails(
    init_db, monkeypatch
):
    import app.services.smart_table_store as store_module

    async def fail_init(*_args, **_kwargs):
        raise RuntimeError("template init failed")

    monkeypatch.setattr(store_module, "init_table_db", fail_init)

    with pytest.raises(RuntimeError, match="template init failed"):
        await create_table_db(
            init_db,
            "Should Roll Back",
            "root-init",
            "v1",
            "ws-init",
            "blank",
        )

    result = await init_db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.workspace_id == "ws-init",
            WorkspaceItem.type == "table",
            WorkspaceItem.name == "Should Roll Back",
        )
    )
    assert result.scalars().first() is None
