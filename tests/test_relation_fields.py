from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.workspace_member import Workspace
from app.services.relation_engine import RelationValidationError, normalize_relation_value
from app.services.smart_table_store import (
    add_field,
    delete_record,
    update_field,
    update_record,
)


@pytest.fixture
async def relation_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'relation-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(Workspace(id="ws-1", name="Workspace 1"))
        session.add(Workspace(id="ws-2", name="Workspace 2"))
        session.add_all(
            [
                WorkspaceItem(
                    id="source-table",
                    workspace_id="ws-1",
                    type="table",
                    name="Source",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="target-table",
                    workspace_id="ws-1",
                    type="table",
                    name="Target",
                    parent_id=None,
                    order_index=1,
                ),
                WorkspaceItem(
                    id="other-table",
                    workspace_id="ws-2",
                    type="table",
                    name="Other",
                    parent_id=None,
                    order_index=0,
                ),
                TableField(
                    id="source-title",
                    table_id="source-table",
                    name="Source title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="target-title",
                    table_id="target-table",
                    name="Target title",
                    type="text",
                    order_index=0,
                ),
                TableRecord(
                    id="s1",
                    table_id="source-table",
                    data={"source-title": "Source row"},
                    order_index=0,
                ),
                TableRecord(
                    id="t1",
                    table_id="target-table",
                    data={"target-title": "Target one"},
                    order_index=0,
                ),
                TableRecord(
                    id="t2",
                    table_id="target-table",
                    data={"target-title": "Target two"},
                    order_index=1,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


def relation_field(*, multiple: bool = True, target: str = "target-table"):
    return {
        "id": "linked",
        "name": "Linked records",
        "type": "relation",
        "property": {
            "targetTableId": target,
            "displayFieldId": "target-title" if target == "target-table" else None,
            "multiple": multiple,
        },
    }


def test_relation_value_normalization():
    multi = relation_field(multiple=True)
    assert normalize_relation_value(multi, ["t1", "t1", "t2"]) == ["t1", "t2"]
    assert normalize_relation_value(multi, None) == []

    single = relation_field(multiple=False)
    assert normalize_relation_value(single, "t1") == "t1"
    assert normalize_relation_value(single, None) is None
    with pytest.raises(RelationValidationError):
        normalize_relation_value(single, ["t1", "t2"])


@pytest.mark.asyncio
async def test_relation_field_requires_same_workspace(relation_db):
    with pytest.raises(RelationValidationError, match="same workspace"):
        await add_field(
            relation_db,
            "source-table",
            relation_field(target="other-table"),
        )


@pytest.mark.asyncio
async def test_relation_write_validates_targets_and_deduplicates(relation_db):
    await add_field(relation_db, "source-table", relation_field())

    updated = await update_record(
        relation_db,
        "source-table",
        "s1",
        "linked",
        ["t1", "t1", "t2"],
    )
    assert updated is not None
    assert updated["linked"] == ["t1", "t2"]

    with pytest.raises(RelationValidationError, match="does not exist"):
        await update_record(
            relation_db,
            "source-table",
            "s1",
            "linked",
            ["missing"],
        )


@pytest.mark.asyncio
async def test_deleting_target_record_removes_reverse_links(relation_db):
    await add_field(relation_db, "source-table", relation_field())
    await update_record(
        relation_db,
        "source-table",
        "s1",
        "linked",
        ["t1", "t2"],
    )

    assert await delete_record(relation_db, "target-table", "t1") is True

    source = await relation_db.get(TableRecord, ("s1", "source-table"))
    assert source is not None
    assert source.data["linked"] == ["t2"]


@pytest.mark.asyncio
async def test_single_relation_rejects_multiple_targets(relation_db):
    await add_field(relation_db, "source-table", relation_field(multiple=False))
    with pytest.raises(RelationValidationError, match="only one"):
        await update_record(
            relation_db,
            "source-table",
            "s1",
            "linked",
            ["t1", "t2"],
        )


@pytest.mark.asyncio
async def test_relation_field_update_migrates_safe_cardinality_change(relation_db):
    await add_field(relation_db, "source-table", relation_field(multiple=True))
    await update_record(
        relation_db,
        "source-table",
        "s1",
        "linked",
        ["t1"],
    )

    updated_field = await update_field(
        relation_db,
        "source-table",
        "linked",
        {"property": relation_field(multiple=False)["property"]},
    )
    assert updated_field is not None

    source = await relation_db.get(TableRecord, ("s1", "source-table"))
    assert source is not None
    assert source.data["linked"] == "t1"


@pytest.mark.asyncio
async def test_relation_field_update_rejects_unsafe_cardinality_change(relation_db):
    await add_field(relation_db, "source-table", relation_field(multiple=True))
    await update_record(
        relation_db,
        "source-table",
        "s1",
        "linked",
        ["t1", "t2"],
    )

    with pytest.raises(RelationValidationError, match="only one"):
        await update_field(
            relation_db,
            "source-table",
            "linked",
            {"property": relation_field(multiple=False)["property"]},
        )
