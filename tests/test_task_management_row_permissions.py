from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from graphql import GraphQLError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.row_permissions import set_row_permission_policy
from app.skills.task_management.graphql_api import TaskManagementQuery
from app.skills.task_management.postgres_queries import (
    pg_calculate_project_progress,
    pg_detect_blocking_tasks,
    pg_get_member_workload,
    pg_get_overdue_tasks,
    pg_predict_project_delay,
)


@pytest.fixture
async def task_management_row_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'task-management-row.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        alice = User(
            id=301,
            email="alice-row@example.test",
            name="Alice",
            password_hash="!test",
        )
        bob = User(
            id=302,
            email="bob-row@example.test",
            name="Bob",
            password_hash="!test",
        )
        workspace = Workspace(id="ws-task-row", name="Task Row Scope")
        table = WorkspaceItem(
            id="table-task-row",
            workspace_id=workspace.id,
            type="table",
            name="Tasks",
            parent_id=None,
            order_index=0,
        )
        session.add_all(
            [
                alice,
                bob,
                workspace,
                WorkspaceMember(
                    user_id=alice.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=bob.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.editor,
                ),
                table,
            ]
        )

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        session.add_all(
            [
                TableRecord(
                    id="alice-task",
                    table_id=table.id,
                    data={
                        "title": "Alice overdue",
                        "status": "进行中",
                        "due": yesterday,
                        "assignee": "301",
                        "progress": 20,
                    },
                    order_index=1,
                    created_by_user_id=alice.id,
                ),
                TableRecord(
                    id="bob-task",
                    table_id=table.id,
                    data={
                        "title": "Bob private",
                        "status": "blocked",
                        "due": yesterday,
                        "assignee": "302",
                        "progress": 0,
                    },
                    order_index=2,
                    created_by_user_id=bob.id,
                ),
                TableRecord(
                    id="bob-future",
                    table_id=table.id,
                    data={
                        "title": "Bob future",
                        "status": "进行中",
                        "due": tomorrow,
                        "assignee": "302",
                        "progress": 40,
                    },
                    order_index=3,
                    created_by_user_id=bob.id,
                ),
            ]
        )
        await session.commit()
        await set_row_permission_policy(session, table.id, mode="creator")
        yield session, alice, bob, table

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_management_analytics_only_use_visible_creator_rows(
    task_management_row_db,
):
    session, alice, _, table = task_management_row_db

    overdue = await pg_get_overdue_tasks(
        session,
        table.id,
        user_id=alice.id,
        limit=100,
    )
    assert [task["recordId"] for task in overdue] == ["alice-task"]

    progress = await pg_calculate_project_progress(
        session,
        table.id,
        user_id=alice.id,
    )
    assert progress["totalTasks"] == 1
    assert progress["overdueTasks"] == 1

    workload = await pg_get_member_workload(
        session,
        table.id,
        user_id=alice.id,
    )
    visible_member_ids = {
        row["memberId"] for row in workload if row["memberId"] != "_unassigned"
    }
    assert visible_member_ids == {"301"}

    blockers = await pg_detect_blocking_tasks(
        session,
        table.id,
        user_id=alice.id,
    )
    assert all(task["recordId"] != "bob-task" for task in blockers)

    prediction = await pg_predict_project_delay(
        session,
        table.id,
        user_id=alice.id,
    )
    assert prediction["totalTasks"] == 1


@pytest.mark.asyncio
async def test_task_management_manager_scope_still_sees_all_rows(
    task_management_row_db,
):
    session, alice, _, table = task_management_row_db
    membership = await session.get(
        WorkspaceMember,
        {"user_id": alice.id, "workspace_id": table.workspace_id},
    )
    membership.role = WorkspaceRole.owner
    await session.commit()

    progress = await pg_calculate_project_progress(
        session,
        table.id,
        user_id=alice.id,
    )
    assert progress["totalTasks"] == 3


@pytest.mark.asyncio
async def test_all_task_management_graphql_analytics_forward_authenticated_user_scope(
    monkeypatch,
):
    captured_permissions: list[tuple[str, str]] = []
    captured_calls: list[tuple[str, object, str, str | None, int | None]] = []

    async def allow_read(info, table_id, required):
        captured_permissions.append((table_id, required))
        return "read"

    async def fake_overdue(
        db,
        table_id,
        *,
        workspace_id=None,
        user_id=None,
        status_not_in=None,
        limit=100,
        **kwargs,
    ):
        captured_calls.append(("overdue", db, table_id, workspace_id, user_id))
        return []

    async def fake_progress(db, table_id, *, workspace_id=None, user_id=None, **kwargs):
        captured_calls.append(("progress", db, table_id, workspace_id, user_id))
        return {"totalTasks": 1}

    async def fake_workload(db, table_id, *, workspace_id=None, user_id=None, **kwargs):
        captured_calls.append(("workload", db, table_id, workspace_id, user_id))
        return []

    async def fake_blocking(db, table_id, *, workspace_id=None, user_id=None, **kwargs):
        captured_calls.append(("blocking", db, table_id, workspace_id, user_id))
        return []

    async def fake_prediction(db, table_id, *, workspace_id=None, user_id=None, **kwargs):
        captured_calls.append(("prediction", db, table_id, workspace_id, user_id))
        return {"totalTasks": 1}

    monkeypatch.setattr(
        "app.skills.task_management.graphql_api._require_item_permission",
        allow_read,
    )
    monkeypatch.setattr(
        "app.skills.task_management.postgres_queries.pg_get_overdue_tasks",
        fake_overdue,
    )
    monkeypatch.setattr(
        "app.skills.task_management.postgres_queries.pg_calculate_project_progress",
        fake_progress,
    )
    monkeypatch.setattr(
        "app.skills.task_management.postgres_queries.pg_get_member_workload",
        fake_workload,
    )
    monkeypatch.setattr(
        "app.skills.task_management.postgres_queries.pg_detect_blocking_tasks",
        fake_blocking,
    )
    monkeypatch.setattr(
        "app.skills.task_management.postgres_queries.pg_predict_project_delay",
        fake_prediction,
    )

    fake_db = object()
    info = SimpleNamespace(
        context={
            "db": fake_db,
            "user": SimpleNamespace(id=777),
        }
    )
    query = TaskManagementQuery()

    await query.task_management_overdue_tasks(
        info,
        table_id="table-scoped",
        workspace_id="workspace-scoped",
    )
    await query.task_management_project_progress(
        info,
        table_id="table-scoped",
        workspace_id="workspace-scoped",
    )
    await query.task_management_member_workload(
        info,
        table_id="table-scoped",
        workspace_id="workspace-scoped",
    )
    await query.task_management_blocking_tasks(
        info,
        table_id="table-scoped",
        workspace_id="workspace-scoped",
    )
    await query.task_management_delay_prediction(
        info,
        table_id="table-scoped",
        workspace_id="workspace-scoped",
    )

    assert captured_permissions == [("table-scoped", "read")] * 5
    assert [call[0] for call in captured_calls] == [
        "overdue",
        "progress",
        "workload",
        "blocking",
        "prediction",
    ]
    for _, db, table_id, workspace_id, user_id in captured_calls:
        assert db is fake_db
        assert table_id == "table-scoped"
        assert workspace_id == "workspace-scoped"
        assert user_id == 777


@pytest.mark.asyncio
async def test_all_task_management_graphql_analytics_reject_anonymous_access():
    info = SimpleNamespace(context={"db": object(), "user": None})
    query = TaskManagementQuery()

    calls = [
        lambda: query.task_management_overdue_tasks(info, table_id="table-private"),
        lambda: query.task_management_project_progress(info, table_id="table-private"),
        lambda: query.task_management_member_workload(info, table_id="table-private"),
        lambda: query.task_management_blocking_tasks(info, table_id="table-private"),
        lambda: query.task_management_delay_prediction(info, table_id="table-private"),
    ]

    for call in calls:
        with pytest.raises(GraphQLError, match="Unauthorized"):
            await call()
