from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.db.init_db import _sync_ensure_schema
from app.models.change_history import ChangeItem, ChangeSet, RecycleBinRecord
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.change_history import (
    ChangeConflictError,
    list_record_history,
    list_recycle_bin,
    purge_recycled_record,
    undo_change_set,
)
from app.services.smart_table_store import (
    add_field,
    apply_record_patches,
    delete_record,
    update_record,
)


@pytest.fixture
async def history_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'change-history-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                User(
                    id=7,
                    email="history@example.com",
                    password_hash="test-only",
                    name="History Tester",
                ),
                Workspace(
                    id="workspace-history",
                    name="History Workspace",
                ),
                WorkspaceMember(
                    user_id=7,
                    workspace_id="workspace-history",
                    role=WorkspaceRole.owner,
                ),
                WorkspaceItem(
                    id="root-history",
                    workspace_id="workspace-history",
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="table-history",
                    workspace_id="workspace-history",
                    type="table",
                    name="History",
                    parent_id="root-history",
                    order_index=0,
                    default_view_id="v1",
                ),
                WorkspaceItem(
                    id="source-table",
                    workspace_id="workspace-history",
                    type="table",
                    name="Source",
                    parent_id="root-history",
                    order_index=1,
                    default_view_id="v1",
                ),
                WorkspaceItem(
                    id="target-table",
                    workspace_id="workspace-history",
                    type="table",
                    name="Target",
                    parent_id="root-history",
                    order_index=2,
                    default_view_id="v1",
                ),
                TableField(
                    id="title",
                    table_id="table-history",
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id="table-history",
                    name="Status",
                    type="text",
                    order_index=1,
                ),
                TableField(
                    id="source-title",
                    table_id="source-table",
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="target-title",
                    table_id="target-table",
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableRecord(
                    id="r1",
                    table_id="table-history",
                    data={"title": "Alpha", "status": "todo"},
                    order_index=1,
                    created_by_user_id=7,
                    version=1,
                ),
                TableRecord(
                    id="r2",
                    table_id="table-history",
                    data={"title": "Beta", "status": "todo"},
                    order_index=2,
                    created_by_user_id=7,
                    version=1,
                ),
                TableRecord(
                    id="s1",
                    table_id="source-table",
                    data={"source-title": "Source"},
                    order_index=1,
                    created_by_user_id=7,
                    version=1,
                ),
                TableRecord(
                    id="t1",
                    table_id="target-table",
                    data={"target-title": "Target"},
                    order_index=1,
                    created_by_user_id=7,
                    version=1,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


async def _record(session, table_id: str, record_id: str):
    result = await session.execute(
        select(TableRecord).where(
            TableRecord.table_id == table_id,
            TableRecord.id == record_id,
        )
    )
    return result.scalars().first()


async def _latest_change_set(session, operation: str):
    result = await session.execute(
        select(ChangeSet)
        .where(ChangeSet.operation == operation)
        .order_by(ChangeSet.created_at.desc(), ChangeSet.id.desc())
        .limit(1)
    )
    return result.scalars().first()


def _relation_field():
    return {
        "id": "linked",
        "name": "Linked",
        "type": "relation",
        "property": {
            "targetTableId": "target-table",
            "displayFieldId": "target-title",
            "multiple": True,
        },
    }


@pytest.mark.asyncio
async def test_update_creates_history_version_and_ignores_noop(history_db):
    updated = await update_record(
        history_db,
        "table-history",
        "r1",
        "title",
        "Alpha updated",
        user_id=7,
        source="test",
    )
    assert updated["title"] == "Alpha updated"

    record = await _record(history_db, "table-history", "r1")
    assert record is not None
    assert record.version == 2

    history = await list_record_history(
        history_db,
        table_id="table-history",
        record_id="r1",
    )
    assert history["totalCount"] == 1
    item = history["items"][0]
    assert item["changeSet"]["operation"] == "update"
    assert item["changeSet"]["actorId"] == 7
    assert item["changeSet"]["source"] == "test"
    assert item["before"]["title"] == "Alpha"
    assert item["after"]["title"] == "Alpha updated"
    assert item["changedFields"] == ["title"]
    assert item["versionBefore"] == 1
    assert item["versionAfter"] == 2

    # A repeated assignment is not a logical change and must not inflate
    # history or optimistic versions.
    await update_record(
        history_db,
        "table-history",
        "r1",
        "title",
        "Alpha updated",
        user_id=7,
        source="test",
    )
    record = await _record(history_db, "table-history", "r1")
    assert record.version == 2
    history = await list_record_history(
        history_db,
        table_id="table-history",
        record_id="r1",
    )
    assert history["totalCount"] == 1


@pytest.mark.asyncio
async def test_batch_patch_is_one_changeset_and_one_version_increment_per_row(history_db):
    changed = await apply_record_patches(
        history_db,
        "table-history",
        [
            {"recordId": "r1", "fieldId": "title", "value": "Alpha 2"},
            {"recordId": "r1", "fieldId": "status", "value": "doing"},
            {"recordId": "r2", "fieldId": "status", "value": "done"},
        ],
        user_id=7,
        source="realtime",
    )
    assert changed == 2

    result = await history_db.execute(
        select(ChangeSet).where(ChangeSet.operation == "batch_update")
    )
    change_sets = list(result.scalars().all())
    assert len(change_sets) == 1
    assert change_sets[0].source == "realtime"

    item_result = await history_db.execute(
        select(ChangeItem)
        .where(ChangeItem.change_set_id == change_sets[0].id)
        .order_by(ChangeItem.entity_id.asc())
    )
    items = list(item_result.scalars().all())
    assert [item.entity_id for item in items] == ["r1", "r2"]
    assert items[0].changed_fields == ["status", "title"]
    assert items[0].version_before == 1
    assert items[0].version_after == 2
    assert items[1].changed_fields == ["status"]

    r1 = await _record(history_db, "table-history", "r1")
    r2 = await _record(history_db, "table-history", "r2")
    assert r1.version == 2
    assert r2.version == 2


@pytest.mark.asyncio
async def test_undo_batch_restores_all_rows_atomically(history_db):
    await apply_record_patches(
        history_db,
        "table-history",
        [
            {"recordId": "r1", "fieldId": "status", "value": "doing"},
            {"recordId": "r2", "fieldId": "status", "value": "done"},
        ],
        user_id=7,
        source="test",
    )
    original = await _latest_change_set(history_db, "batch_update")
    assert original is not None

    result = await undo_change_set(
        history_db,
        change_set_id=original.id,
        actor_id=7,
        source="test",
    )
    assert result["affectedCount"] == 2
    assert result["affectedTableIds"] == ["table-history"]

    r1 = await _record(history_db, "table-history", "r1")
    r2 = await _record(history_db, "table-history", "r2")
    assert r1.data["status"] == "todo"
    assert r2.data["status"] == "todo"
    assert r1.version == 3
    assert r2.version == 3

    await history_db.refresh(original)
    assert original.status == "undone"
    undo_set = await _latest_change_set(history_db, "undo")
    assert undo_set is not None
    assert undo_set.parent_change_set_id == original.id


@pytest.mark.asyncio
async def test_undo_conflict_never_overwrites_newer_changes(history_db):
    await update_record(
        history_db,
        "table-history",
        "r1",
        "title",
        "Version 2",
        user_id=7,
        source="test",
    )
    first = await _latest_change_set(history_db, "update")
    assert first is not None

    await update_record(
        history_db,
        "table-history",
        "r1",
        "title",
        "Version 3",
        user_id=7,
        source="test",
    )

    with pytest.raises(ChangeConflictError, match="newer changes"):
        await undo_change_set(
            history_db,
            change_set_id=first.id,
            actor_id=7,
            source="test",
        )

    current = await _record(history_db, "table-history", "r1")
    assert current.data["title"] == "Version 3"
    assert current.version == 3
    await history_db.refresh(first)
    assert first.status == "applied"


@pytest.mark.asyncio
async def test_delete_recycle_and_undo_restore_reverse_relations(history_db):
    await add_field(history_db, "source-table", _relation_field())
    await update_record(
        history_db,
        "source-table",
        "s1",
        "linked",
        ["t1"],
        user_id=7,
        source="test",
    )
    source_before_delete = await _record(history_db, "source-table", "s1")
    assert source_before_delete.version == 2
    assert source_before_delete.data["linked"] == ["t1"]

    assert (
        await delete_record(
            history_db,
            "target-table",
            "t1",
            user_id=7,
            source="test",
        )
        is True
    )

    assert await _record(history_db, "target-table", "t1") is None
    source_after_delete = await _record(history_db, "source-table", "s1")
    assert source_after_delete.data["linked"] == []
    assert source_after_delete.version == 3

    recycle = await list_recycle_bin(history_db, table_id="target-table")
    assert recycle["totalCount"] == 1
    assert recycle["items"][0]["recordId"] == "t1"
    delete_set = await _latest_change_set(history_db, "delete")
    assert delete_set is not None

    item_result = await history_db.execute(
        select(ChangeItem).where(ChangeItem.change_set_id == delete_set.id)
    )
    delete_items = list(item_result.scalars().all())
    assert {(item.table_id, item.entity_id) for item in delete_items} == {
        ("target-table", "t1"),
        ("source-table", "s1"),
    }

    undo_result = await undo_change_set(
        history_db,
        change_set_id=delete_set.id,
        actor_id=7,
        source="test",
    )
    assert undo_result["affectedTableIds"] == ["source-table", "target-table"]

    restored = await _record(history_db, "target-table", "t1")
    source_restored = await _record(history_db, "source-table", "s1")
    assert restored is not None
    assert restored.data["target-title"] == "Target"
    assert restored.version == 3
    assert source_restored.data["linked"] == ["t1"]
    assert source_restored.version == 4

    recycle = await list_recycle_bin(history_db, table_id="target-table")
    assert recycle["totalCount"] == 0


@pytest.mark.asyncio
async def test_permanent_purge_removes_all_recycle_snapshots_and_redacts_history(history_db):
    # First delete/restore creates an old recycle snapshot with status=restored.
    assert await delete_record(
        history_db,
        "table-history",
        "r1",
        user_id=7,
        source="test",
    )
    first_delete = await _latest_change_set(history_db, "delete")
    assert first_delete is not None
    await undo_change_set(
        history_db,
        change_set_id=first_delete.id,
        actor_id=7,
        source="test",
    )

    # Delete the same logical row again, then permanently purge it.
    assert await delete_record(
        history_db,
        "table-history",
        "r1",
        user_id=7,
        source="test",
    )
    recycle_before_purge = await list_recycle_bin(
        history_db,
        table_id="table-history",
    )
    assert recycle_before_purge["totalCount"] == 1
    second_delete_id = recycle_before_purge["items"][0]["changeSetId"]
    second_delete_result = await history_db.execute(
        select(ChangeSet).where(ChangeSet.id == second_delete_id)
    )
    second_delete = second_delete_result.scalars().one()
    assert second_delete.status == "applied"
    assert await purge_recycled_record(
        history_db,
        table_id="table-history",
        record_id="r1",
        actor_id=7,
    )
    await history_db.refresh(second_delete)
    assert second_delete.status == "irreversible"

    recycle_result = await history_db.execute(
        select(RecycleBinRecord).where(
            RecycleBinRecord.table_id == "table-history",
            RecycleBinRecord.record_id == "r1",
        )
    )
    assert list(recycle_result.scalars().all()) == []

    history_result = await history_db.execute(
        select(ChangeItem).where(
            ChangeItem.table_id == "table-history",
            ChangeItem.entity_id == "r1",
        )
    )
    items = list(history_result.scalars().all())
    assert items
    assert all(item.before_data is None for item in items)
    assert all(item.after_data is None for item in items)
    assert all(item.before_meta is None for item in items)
    # The irreversible purge entry has after_meta={"purged": True}; older
    # entries must be redacted.
    assert all(
        item.after_meta is None or item.after_meta == {"purged": True}
        for item in items
    )


def test_graphql_schema_exposes_history_contract():
    schema_text = schema.as_str()
    assert "recordHistory" in schema_text
    assert "recycleBin" in schema_text
    assert "undoChangeSet" in schema_text
    assert "restoreRecord" in schema_text
    assert "purgeRecord" in schema_text


def test_legacy_table_record_schema_auto_adds_version(tmp_path):
    db_path = tmp_path / "legacy-schema.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE table_records (
                        id VARCHAR NOT NULL,
                        table_id VARCHAR NOT NULL,
                        data JSON NOT NULL,
                        order_index INTEGER,
                        created_by_user_id INTEGER,
                        PRIMARY KEY (id, table_id)
                    )
                    """
                )
            )
            _sync_ensure_schema(connection)

        columns = {column["name"] for column in inspect(engine).get_columns("table_records")}
        assert "version" in columns
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO table_records
                        (id, table_id, data, order_index, created_by_user_id)
                    VALUES
                        ('legacy', 'legacy-table', '{}', 0, NULL)
                    """
                )
            )
            version = connection.execute(
                text(
                    "SELECT version FROM table_records WHERE id='legacy' AND table_id='legacy-table'"
                )
            ).scalar_one()
            assert version == 1
    finally:
        engine.dispose()
