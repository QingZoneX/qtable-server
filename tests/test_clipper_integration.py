from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.clipper import (
    CreateClipperTask,
    UpdateClipperTaskStatus,
    create_clipper_task,
    get_clipper_task_status,
    list_clipper_tables,
    update_clipper_task_status,
)
from app.db.base import Base
from app.models.clipper_integration import ClipperTaskReceipt
from app.models.smart_table import TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.row_permissions import set_row_permission_policy


@pytest.fixture
async def clipper_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'clipper-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(
            id=101,
            email="owner@example.test",
            name="Owner",
            password_hash="!test",
        )
        workspace = Workspace(id="workspace-test", name="Test Workspace")
        session.add_all(
            [
                user,
                workspace,
                WorkspaceMember(
                    user_id=user.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.owner,
                ),
                WorkspaceItem(
                    id="table-test",
                    workspace_id=workspace.id,
                    type="table",
                    name="Tasks",
                    parent_id=None,
                    order_index=0,
                ),
            ]
        )
        await session.commit()
        yield session, user

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_creation_is_durable_and_idempotent(clipper_db):
    session, user = clipper_db
    payload = CreateClipperTask(
        annotation_id="annotation-1",
        annotation_ids=["annotation-1", "annotation-2"],
        title="Follow up on captured context",
        selected_text="Relevant excerpt",
        page_url="https://example.test/article",
        page_title="Article",
        target_table_id="table-test",
        client_mutation_id="mutation-1",
        source={
            "type": "qnote_annotation",
            "workspaceId": "workspace-test",
            "annotationIds": ["annotation-1", "annotation-2"],
        },
    )

    first = await create_clipper_task(payload, user=user, db=session)
    retried = await create_clipper_task(payload, user=user, db=session)

    assert retried == first
    assert first["record_id"] == first["task_id"]
    assert first["qtable_url"].endswith(
        f"/table/table-test?row={first['record_id']}"
    )
    record_count = await session.scalar(select(func.count()).select_from(TableRecord))
    receipt_count = await session.scalar(
        select(func.count()).select_from(ClipperTaskReceipt)
    )
    assert record_count == 1
    assert receipt_count == 1
    created_record = (
        await session.execute(
            select(TableRecord).where(TableRecord.id == first["record_id"])
        )
    ).scalars().one()
    assert created_record.created_by_user_id == user.id

    with pytest.raises(HTTPException) as reused:
        await create_clipper_task(
            payload.model_copy(update={"title": "Different request"}),
            user=user,
            db=session,
        )
    assert reused.value.status_code == 409

    second = await create_clipper_task(
        payload.model_copy(update={"client_mutation_id": "mutation-2"}),
        user=user,
        db=session,
    )
    assert second["task_id"] != first["task_id"]
    record_count = await session.scalar(select(func.count()).select_from(TableRecord))
    assert record_count == 2


@pytest.mark.asyncio
async def test_task_creation_rejects_cross_workspace_annotation_link(clipper_db):
    session, user = clipper_db
    other_workspace = Workspace(id="workspace-other", name="Other Workspace")
    other_table = WorkspaceItem(
        id="table-other",
        workspace_id=other_workspace.id,
        type="table",
        name="Other tasks",
        parent_id=None,
        order_index=0,
    )
    session.add_all([
        other_workspace,
        WorkspaceMember(
            user_id=user.id,
            workspace_id=other_workspace.id,
            role=WorkspaceRole.owner,
        ),
        other_table,
    ])
    await session.commit()

    payload = CreateClipperTask(
        annotation_id="annotation-1",
        title="Invalid cross-workspace task",
        target_table_id=other_table.id,
        source={"type": "qnote_annotation", "workspaceId": "workspace-test"},
    )
    with pytest.raises(HTTPException) as error:
        await create_clipper_task(payload, user=user, db=session)
    assert error.value.status_code == 400
    assert "same workspace" in str(error.value.detail)


@pytest.mark.asyncio
async def test_clipper_respects_row_scope_for_counts_status_reads_and_updates(clipper_db):
    session, owner = clipper_db
    payload = CreateClipperTask(
        annotation_id="owner-annotation",
        title="Owner-only task",
        target_table_id="table-test",
        client_mutation_id="owner-task",
        source={"type": "qnote_annotation", "workspaceId": "workspace-test"},
    )
    created = await create_clipper_task(payload, user=owner, db=session)

    member = User(
        id=202,
        email="member@example.test",
        name="Member",
        password_hash="!test",
    )
    session.add(member)
    session.add(
        WorkspaceMember(
            user_id=member.id,
            workspace_id="workspace-test",
            role=WorkspaceRole.editor,
        )
    )
    await session.commit()

    await set_row_permission_policy(
        session,
        "table-test",
        mode="creator",
    )

    # FastAPI resolves Query(default=None) to None at request time. These tests
    # call the endpoint function directly, so pass None explicitly instead of
    # leaving the FastAPI Query descriptor as the Python default value.
    owner_tables = await list_clipper_tables(
        workspace_id=None,
        user=owner,
        db=session,
    )
    member_tables = await list_clipper_tables(
        workspace_id=None,
        user=member,
        db=session,
    )
    assert owner_tables[0]["row_count"] == 1
    assert member_tables[0]["row_count"] == 0

    with pytest.raises(HTTPException) as hidden_read:
        await get_clipper_task_status(
            created["task_id"],
            target_table_id="table-test",
            user=member,
            db=session,
        )
    assert hidden_read.value.status_code == 404

    with pytest.raises(HTTPException) as hidden_update:
        await update_clipper_task_status(
            created["task_id"],
            UpdateClipperTaskStatus(
                target_table_id="table-test",
                value="已完成",
            ),
            user=member,
            db=session,
        )
    assert hidden_update.value.status_code == 404

    owner_status = await get_clipper_task_status(
        created["task_id"],
        target_table_id="table-test",
        user=owner,
        db=session,
    )
    assert owner_status["task_id"] == created["task_id"]
