from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, TableView, WorkspaceItem
from app.models.task_profile import TableTaskProfile
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.change_history import append_change_set
from app.services.collaboration.assignment_repair import (
    ASSIGNMENT_REPAIR_BATCH_SIZE,
    repair_assignment_notifications_for_user,
)
from app.services.collaboration.notifications import (
    list_notifications,
    sync_assignment_notifications_for_table,
)


ALICE = 125101
BOB = 125102
WORKSPACE_ID = "ws-assignment-repair-test"
TABLE_ID = "tbl-assignment-repair-test"
RECORD_ID = "record-assignment-repair-test"


@pytest_asyncio.fixture
async def repair_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                User(
                    id=ALICE,
                    email="assignment-repair-alice@example.test",
                    name="Alice",
                    password_hash="!test",
                ),
                User(
                    id=BOB,
                    email="assignment-repair-bob@example.test",
                    name="Bob",
                    password_hash="!test",
                ),
                Workspace(id=WORKSPACE_ID, name="Assignment Repair"),
                WorkspaceItem(
                    id="root-assignment-repair-test",
                    workspace_id=WORKSPACE_ID,
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id=TABLE_ID,
                    workspace_id=WORKSPACE_ID,
                    type="table",
                    name="Tasks",
                    parent_id="root-assignment-repair-test",
                    order_index=1,
                    default_view_id="v1",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=ALICE,
                    workspace_id=WORKSPACE_ID,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=BOB,
                    workspace_id=WORKSPACE_ID,
                    role=WorkspaceRole.editor,
                ),
                TableField(
                    id="title",
                    table_id=TABLE_ID,
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="owner",
                    table_id=TABLE_ID,
                    name="Owner",
                    type="member",
                    property={"multiple": True},
                    order_index=1,
                ),
                TableField(
                    id="custom",
                    table_id=TABLE_ID,
                    name="Custom",
                    type="text",
                    order_index=2,
                ),
                TableView(
                    id="v1",
                    table_id=TABLE_ID,
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableTaskProfile(
                    table_id=TABLE_ID,
                    config={
                        "schemaVersion": 1,
                        "titleFieldId": "title",
                        "statusFieldId": None,
                        "assigneeFieldId": "owner",
                        "priorityFieldId": None,
                        "startDateFieldId": None,
                        "dueDateFieldId": None,
                        "progressFieldId": None,
                        "parentFieldId": None,
                        "dependencyFieldId": None,
                        "workloadFieldId": None,
                        "completedStatusValues": [],
                        "blockedStatusValues": [],
                        "notStartedStatusValues": [],
                    },
                    updated_by_user_id=ALICE,
                ),
                TableRecord(
                    id=RECORD_ID,
                    table_id=TABLE_ID,
                    data={
                        "title": "Long offline assignment",
                        "owner": [str(BOB)],
                        "custom": "latest",
                    },
                    order_index=0,
                    created_by_user_id=ALICE,
                    version=1,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_reconnect_repair_finds_assignment_older_than_realtime_window(repair_db):
    assignment = await append_change_set(
        repair_db,
        table_id=TABLE_ID,
        actor_id=ALICE,
        operation="update",
        source="graphql",
        summary="Assign task",
        items=[
            {
                "table_id": TABLE_ID,
                "entity_type": "record",
                "entity_id": RECORD_ID,
                "before_data": {"owner": [str(ALICE)]},
                "after_data": {"owner": [str(BOB)]},
                "changed_fields": ["owner"],
            }
        ],
    )
    # SQLite CURRENT_TIMESTAMP is second-granularity. Make the target event
    # explicitly older so UUID tie-breaking cannot make this test flaky.
    assignment.created_at = datetime.now(timezone.utc) - timedelta(days=1)

    # Push the assignment beyond the realtime scan window. Realtime remains
    # intentionally bounded, while reconnect repair must still find it.
    for index in range(ASSIGNMENT_REPAIR_BATCH_SIZE + 25):
        await append_change_set(
            repair_db,
            table_id=TABLE_ID,
            actor_id=ALICE,
            operation="update",
            source="graphql",
            summary=f"Unrelated update {index}",
            items=[
                {
                    "table_id": TABLE_ID,
                    "entity_type": "record",
                    "entity_id": RECORD_ID,
                    "before_data": {"custom": str(index)},
                    "after_data": {"custom": str(index + 1)},
                    "changed_fields": ["custom"],
                }
            ],
        )
    await repair_db.commit()

    assert await sync_assignment_notifications_for_table(
        repair_db,
        table_id=TABLE_ID,
    ) == []

    repaired = await repair_assignment_notifications_for_user(
        repair_db,
        user_id=BOB,
    )
    assert repaired == 1
    assert await repair_assignment_notifications_for_user(
        repair_db,
        user_id=BOB,
    ) == 0

    feed = await list_notifications(
        repair_db,
        user_id=BOB,
        types=["task_assigned"],
    )
    assert feed["totalCount"] == 1
    assert feed["items"][0]["title"] == "Long offline assignment"
    assert feed["items"][0]["actor"]["id"] == ALICE
    assert feed["items"][0]["recordId"] == RECORD_ID
    assert feed["items"][0]["payload"]["source"] == "graphql"
    assert feed["items"][0]["id"]
    assert assignment.id
