from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.qnote_integration import get_qnote_task_snapshot
from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.task_profile import TableTaskProfile
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.change_history import append_change_set, record_meta
from app.services.row_permissions import set_row_permission_policy


@pytest.fixture
async def snapshot_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'qnote-task-snapshot.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        owner = User(
            id=301,
            email="owner@example.test",
            name="Owner",
            password_hash="!test",
        )
        member = User(
            id=302,
            email="member@example.test",
            name="Project Member",
            password_hash="!test",
        )
        workspace = Workspace(id="workspace-snapshot", name="Snapshot Workspace")
        table = WorkspaceItem(
            id="table-snapshot",
            workspace_id=workspace.id,
            type="table",
            name="Tasks",
            parent_id=None,
            order_index=0,
        )
        session.add_all(
            [
                owner,
                member,
                workspace,
                WorkspaceMember(
                    user_id=owner.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.owner,
                ),
                WorkspaceMember(
                    user_id=member.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.editor,
                ),
                table,
                TableField(
                    id="f-title",
                    table_id=table.id,
                    name="任意重命名标题",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="f-status",
                    table_id=table.id,
                    name="任意重命名状态",
                    type="select",
                    options=[
                        {"id": "todo", "label": "待处理"},
                        {"id": "done", "label": "已完成"},
                    ],
                    order_index=1,
                ),
                TableField(
                    id="f-owner",
                    table_id=table.id,
                    name="任意重命名负责人",
                    type="member",
                    property={"multiple": False},
                    order_index=2,
                ),
                TableField(
                    id="f-priority",
                    table_id=table.id,
                    name="任意重命名优先级",
                    type="select",
                    options=[
                        {"id": "high", "label": "高"},
                        {"id": "low", "label": "低"},
                    ],
                    order_index=3,
                ),
                TableField(
                    id="f-due",
                    table_id=table.id,
                    name="任意重命名截止日期",
                    type="date",
                    order_index=4,
                ),
                TableTaskProfile(
                    table_id=table.id,
                    config={
                        "schemaVersion": 1,
                        "titleFieldId": "f-title",
                        "statusFieldId": "f-status",
                        "assigneeFieldId": "f-owner",
                        "priorityFieldId": "f-priority",
                        "startDateFieldId": None,
                        "dueDateFieldId": "f-due",
                        "progressFieldId": None,
                        "parentFieldId": None,
                        "dependencyFieldId": None,
                        "workloadFieldId": None,
                        "completedStatusValues": ["done"],
                        "blockedStatusValues": [],
                        "notStartedStatusValues": ["todo"],
                    },
                ),
            ]
        )
        await session.flush()

        record = TableRecord(
            id="record-snapshot",
            table_id=table.id,
            data={
                "f-title": "Trace a real task",
                "f-status": "todo",
                "f-owner": str(member.id),
                "f-priority": "high",
                "f-due": "2026-09-20",
            },
            order_index=1,
            created_by_user_id=owner.id,
            version=2,
        )
        session.add(record)
        await session.flush()
        await append_change_set(
            session,
            table_id=table.id,
            actor_id=owner.id,
            operation="update",
            source="test",
            summary="Update task",
            items=[
                {
                    "table_id": table.id,
                    "entity_type": "record",
                    "entity_id": record.id,
                    "before_data": {"f-status": "todo"},
                    "after_data": dict(record.data),
                    "before_meta": record_meta(record),
                    "after_meta": record_meta(record),
                    "version_before": 1,
                    "version_after": 2,
                    "changed_fields": ["f-status"],
                }
            ],
        )
        await session.commit()
        yield session, owner, member, table, record

    await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_uses_task_profile_and_returns_business_metadata(snapshot_db):
    session, owner, _member, table, record = snapshot_db

    snapshot = await get_qnote_task_snapshot(
        record.id,
        target_table_id=table.id,
        user=owner,
        db=session,
    )

    assert snapshot["task_id"] == record.id
    assert snapshot["record_id"] == record.id
    assert snapshot["workspace_id"] == table.workspace_id
    assert snapshot["title"] == "Trace a real task"
    assert snapshot["status"] == "待处理"
    assert snapshot["assignee"] == "Project Member"
    assert snapshot["priority"] == "高"
    assert snapshot["due_date"] == "2026-09-20"
    assert snapshot["record_version"] == 2
    assert snapshot["updated_at"] is not None
    assert snapshot["qtable_url"].endswith(
        f"/table/{table.id}?row={record.id}"
    )


@pytest.mark.asyncio
async def test_snapshot_hides_metadata_when_row_is_not_visible(snapshot_db):
    session, _owner, member, table, record = snapshot_db
    await set_row_permission_policy(session, table.id, mode="creator")

    with pytest.raises(HTTPException) as hidden:
        await get_qnote_task_snapshot(
            record.id,
            target_table_id=table.id,
            user=member,
            db=session,
        )

    assert hidden.value.status_code == 404
    assert hidden.value.detail == "QTable task is unavailable"


@pytest.mark.asyncio
async def test_profile_is_authoritative_instead_of_guessing_renamed_fields(snapshot_db):
    session, owner, _member, table, record = snapshot_db
    profile = await session.get(TableTaskProfile, table.id)
    assert profile is not None
    profile.config = {
        **dict(profile.config or {}),
        "titleFieldId": None,
        "assigneeFieldId": None,
    }
    await session.commit()

    snapshot = await get_qnote_task_snapshot(
        record.id,
        target_table_id=table.id,
        user=owner,
        db=session,
    )

    assert snapshot["title"] is None
    assert snapshot["assignee"] is None
    assert snapshot["status"] == "待处理"
