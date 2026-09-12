from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.member_field import (
    MemberFieldValidationError,
    hydrate_member_fields_for_table,
    workspace_member_options_for_table,
)
from app.services.smart_table_store.db_backend import add_field, update_field, update_record


@pytest.fixture
async def member_field_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'member-field.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                User(id=1, email="alice@example.test", password_hash="x", name="Alice"),
                User(id=2, email="bob@example.test", password_hash="x", name="Bob"),
                User(id=3, email="outside@example.test", password_hash="x", name="Outside"),
                Workspace(id="w1", name="Workspace"),
                WorkspaceMember(user_id=1, workspace_id="w1", role=WorkspaceRole.owner),
                WorkspaceMember(user_id=2, workspace_id="w1", role=WorkspaceRole.editor),
                WorkspaceItem(
                    id="root",
                    workspace_id="w1",
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                    default_view_id=None,
                ),
                WorkspaceItem(
                    id="tasks",
                    workspace_id="w1",
                    type="table",
                    name="Tasks",
                    parent_id="root",
                    order_index=1,
                    default_view_id=None,
                ),
                TableField(
                    id="owner",
                    table_id="tasks",
                    name="人员",
                    type="member",
                    options=[{"id": "fake", "label": "旧快照"}],
                    property={"multiple": True},
                    order_index=0,
                ),
                TableField(
                    id="reviewer",
                    table_id="tasks",
                    name="审核人",
                    type="member",
                    options=None,
                    property={"multiple": False},
                    order_index=1,
                ),
                TableField(
                    id="legacy",
                    table_id="tasks",
                    name="旧字段",
                    type="text",
                    options=None,
                    property=None,
                    order_index=2,
                ),
                TableRecord(
                    id="r1",
                    table_id="tasks",
                    data={"owner": [], "reviewer": None, "legacy": ""},
                    order_index=1,
                    created_by_user_id=1,
                    version=1,
                ),
            ]
        )
        await db.commit()
        yield db
    await engine.dispose()


@pytest.mark.asyncio
async def test_member_options_are_live_workspace_members_only(member_field_db):
    options = await workspace_member_options_for_table(member_field_db, "tasks")
    assert [item["id"] for item in options] == ["1", "2"]
    assert [item["label"] for item in options] == ["Alice", "Bob"]


@pytest.mark.asyncio
async def test_hydration_replaces_stale_persisted_member_options(member_field_db):
    fields = [
        {
            "id": "owner",
            "name": "人员",
            "type": "member",
            "options": [{"id": "fake", "label": "旧快照"}],
            "property": {},
        }
    ]
    hydrated = await hydrate_member_fields_for_table(
        member_field_db, "tasks", fields
    )
    assert [item["id"] for item in hydrated[0]["options"]] == ["1", "2"]
    assert hydrated[0]["property"]["multiple"] is True


@pytest.mark.asyncio
async def test_add_member_field_does_not_persist_candidate_snapshot(member_field_db):
    created = await add_field(
        member_field_db,
        "tasks",
        {
            "id": "approver",
            "name": "批准人",
            "type": "member",
            "options": [{"id": "1", "label": "Alice"}],
            "property": {"multiple": False},
        },
    )
    assert created["options"] is None
    stored = await member_field_db.get(
        TableField, {"id": "approver", "table_id": "tasks"}
    )
    assert stored.options is None
    assert stored.property["multiple"] is False


@pytest.mark.asyncio
async def test_multi_member_write_canonicalizes_legacy_shapes_to_ids(member_field_db):
    updated = await update_record(
        member_field_db,
        "tasks",
        "r1",
        "owner",
        [{"id": "1", "name": "Alice"}, {"userId": 2}],
        user_id=1,
    )
    assert updated["owner"] == ["1", "2"]


@pytest.mark.asyncio
async def test_member_write_rejects_user_outside_workspace(member_field_db):
    with pytest.raises(MemberFieldValidationError, match="not a current workspace member"):
        await update_record(
            member_field_db,
            "tasks",
            "r1",
            "owner",
            ["3"],
            user_id=1,
        )


@pytest.mark.asyncio
async def test_single_member_field_accepts_one_and_rejects_multiple(member_field_db):
    updated = await update_record(
        member_field_db,
        "tasks",
        "r1",
        "reviewer",
        "2",
        user_id=1,
    )
    assert updated["reviewer"] == "2"

    with pytest.raises(MemberFieldValidationError, match="allows only one user"):
        await update_record(
            member_field_db,
            "tasks",
            "r1",
            "reviewer",
            ["1", "2"],
            user_id=1,
        )


@pytest.mark.asyncio
async def test_multi_to_single_fails_when_existing_row_has_multiple_members(member_field_db):
    record = await member_field_db.get(
        TableRecord, {"id": "r1", "table_id": "tasks"}
    )
    record.data = {**record.data, "owner": ["1", "2"]}
    await member_field_db.commit()

    with pytest.raises(MemberFieldValidationError, match="Cannot switch member field"):
        await update_field(
            member_field_db,
            "tasks",
            "owner",
            {"property": {"multiple": False}},
        )


@pytest.mark.asyncio
async def test_non_member_to_member_requires_existing_values_empty(member_field_db):
    record = await member_field_db.get(
        TableRecord, {"id": "r1", "table_id": "tasks"}
    )
    record.data = {**record.data, "legacy": "Alice"}
    await member_field_db.commit()

    with pytest.raises(MemberFieldValidationError, match="existing values to be empty"):
        await update_field(
            member_field_db,
            "tasks",
            "legacy",
            {"type": "member", "property": {"multiple": False}},
        )
