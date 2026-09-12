from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

import app.services.my_work as my_work_service
from app.db.session import AsyncSessionLocal, engine
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    TableView,
    WorkspaceItem,
)
from app.models.task_profile import TableTaskProfile
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.my_work import build_my_work


pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL-specific My Work integration contract",
)


@pytest.mark.asyncio(loop_scope="module")
async def test_my_work_postgresql_json_and_timezone_contract(monkeypatch):
    user_id = 910001
    hidden_user_id = 910002
    workspace_id = "ws-my-work-pg"
    table_id = "tbl-my-work-pg"

    async with AsyncSessionLocal() as session:
        today = datetime.now(timezone.utc).date()
        session.add_all(
            [
                User(
                    id=user_id,
                    email="my-work-pg@example.test",
                    name="PG User",
                    password_hash="!test",
                ),
                User(
                    id=hidden_user_id,
                    email="my-work-pg-hidden@example.test",
                    name="PG Hidden",
                    password_hash="!test",
                ),
                Workspace(id=workspace_id, name="My Work PostgreSQL"),
                WorkspaceItem(
                    id="root-my-work-pg",
                    workspace_id=workspace_id,
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id=table_id,
                    workspace_id=workspace_id,
                    type="table",
                    name="PG Tasks",
                    parent_id="root-my-work-pg",
                    order_index=1,
                    default_view_id="v1",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=hidden_user_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                TableField(
                    id="title",
                    table_id=table_id,
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id=table_id,
                    name="Status",
                    type="select",
                    options=[
                        {"id": "todo", "label": "Todo"},
                        {"id": "doing", "label": "Doing"},
                        {"id": "done", "label": "Done"},
                    ],
                    order_index=1,
                ),
                TableField(
                    id="owner",
                    table_id=table_id,
                    name="Owner",
                    type="member",
                    property={"multiple": True},
                    order_index=2,
                ),
                TableField(
                    id="due",
                    table_id=table_id,
                    name="Due",
                    type="date",
                    order_index=3,
                ),
                TableField(
                    id="progress",
                    table_id=table_id,
                    name="Progress",
                    type="progress",
                    order_index=4,
                ),
                TableView(
                    id="v1",
                    table_id=table_id,
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableTaskProfile(
                    table_id=table_id,
                    config={
                        "schemaVersion": 1,
                        "titleFieldId": "title",
                        "statusFieldId": "status",
                        "assigneeFieldId": "owner",
                        "priorityFieldId": None,
                        "startDateFieldId": None,
                        "dueDateFieldId": "due",
                        "progressFieldId": "progress",
                        "parentFieldId": None,
                        "dependencyFieldId": None,
                        "workloadFieldId": None,
                        "completedStatusValues": ["done"],
                        "blockedStatusValues": [],
                        "notStartedStatusValues": ["todo"],
                    },
                    updated_by_user_id=user_id,
                ),
                TableRowPermissionPolicy(
                    table_id=table_id,
                    mode="member_field",
                    member_field_id="owner",
                ),
                TableRecord(
                    id="pg-overdue",
                    table_id=table_id,
                    data={
                        "title": "Visible overdue",
                        "status": "doing",
                        "owner": [str(user_id)],
                        "due": (today - timedelta(days=1)).isoformat(),
                        "progress": 25,
                    },
                    order_index=0,
                    created_by_user_id=hidden_user_id,
                ),
                TableRecord(
                    id="pg-todo",
                    table_id=table_id,
                    data={
                        "title": "Visible todo",
                        "status": "todo",
                        "owner": [str(user_id)],
                        "due": today.isoformat(),
                        "progress": 0,
                    },
                    order_index=1,
                    created_by_user_id=hidden_user_id,
                ),
                TableRecord(
                    id="pg-hidden",
                    table_id=table_id,
                    data={
                        "title": "Hidden",
                        "status": "doing",
                        "owner": [str(hidden_user_id)],
                        "due": (today - timedelta(days=1)).isoformat(),
                        "progress": 50,
                    },
                    order_index=2,
                    created_by_user_id=hidden_user_id,
                ),
            ]
        )
        await session.commit()

        payload = await build_my_work(
            session,
            user_id=user_id,
            sections=["tasks", "due", "projects"],
            timezone_name="UTC",
            limit=20,
        )

        tasks = payload["sections"]["tasks"]["data"]
        assert tasks["totalCount"] == 2
        assert {item["recordId"] for item in tasks["items"]} == {
            "pg-overdue",
            "pg-todo",
        }

        due = payload["sections"]["due"]["data"]
        assert due["overdue"]["totalCount"] == 1
        assert due["today"]["totalCount"] == 1

        project = payload["sections"]["projects"]["data"]["items"][0]
        assert project["tableId"] == table_id
        assert project["totalTasks"] == 2
        assert project["overdueCount"] == 1
        assert project["progress"] == 12.5

        original_projects_section = my_work_service._projects_section

        async def fail_projects_with_sql_error(
            db,
            **kwargs,
        ):
            await db.execute(
                text("SELECT * FROM qtable_missing_my_work_relation")
            )
            return await original_projects_section(db, **kwargs)

        monkeypatch.setattr(
            my_work_service,
            "_projects_section",
            fail_projects_with_sql_error,
        )
        isolated = await build_my_work(
            session,
            user_id=user_id,
            sections=["tasks", "projects", "kpi"],
            timezone_name="UTC",
            limit=20,
        )
        assert isolated["sections"]["tasks"]["status"] == "ok"
        assert isolated["sections"]["projects"]["status"] == "error"
        assert (
            isolated["sections"]["projects"]["error"]["code"]
            == "SECTION_FAILED"
        )
        # A PostgreSQL statement error aborts the surrounding transaction
        # unless My Work isolates the failing section with a savepoint.
        assert isolated["sections"]["kpi"]["status"] == "ok"
        assert (
            isolated["sections"]["kpi"]["data"]["myIncompleteCount"]
            == 2
        )


@pytest.mark.asyncio(loop_scope="module")
async def test_my_work_postgresql_tolerates_legacy_epoch_milliseconds():
    """Legacy rows stored JS Date.getTime() as a string in date fields.
    Such values must not abort the whole projects/aggregate when the due
    predicate tries to CAST them to TIMESTAMPTZ on PostgreSQL.
    """
    user_id = 910010
    workspace_id = "ws-my-work-pg-epoch"
    table_id = "tbl-my-work-pg-epoch"

    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                User(
                    id=user_id,
                    email="my-work-pg-epoch@example.test",
                    name="PG Epoch User",
                    password_hash="!test",
                ),
                Workspace(id=workspace_id, name="My Work PG Epoch"),
                WorkspaceItem(
                    id="root-my-work-pg-epoch",
                    workspace_id=workspace_id,
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id=table_id,
                    workspace_id=workspace_id,
                    type="table",
                    name="PG Epoch Tasks",
                    parent_id="root-my-work-pg-epoch",
                    order_index=1,
                    default_view_id="v1",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                TableField(id="title", table_id=table_id, name="Title", type="text", order_index=0),
                TableField(id="status", table_id=table_id, name="Status", type="select",
                           options=[{"id": "todo", "label": "Todo"}, {"id": "done", "label": "Done"}], order_index=1),
                TableField(id="due", table_id=table_id, name="Due", type="date", order_index=2),
                TableView(id="v1", table_id=table_id, name="Grid", type="grid", config={}),
                TableTaskProfile(
                    table_id=table_id,
                    config={
                        "schemaVersion": 1,
                        "titleFieldId": "title",
                        "statusFieldId": "status",
                        "assigneeFieldId": None,
                        "priorityFieldId": None,
                        "startDateFieldId": None,
                        "dueDateFieldId": "due",
                        "progressFieldId": None,
                        "parentFieldId": None,
                        "dependencyFieldId": None,
                        "workloadFieldId": None,
                        "completedStatusValues": ["done"],
                        "blockedStatusValues": [],
                        "notStartedStatusValues": ["todo"],
                    },
                    updated_by_user_id=user_id,
                ),
                TableRecord(
                    id="pg-epoch-bad",
                    table_id=table_id,
                    data={"title": "Legacy ms", "status": "todo", "due": "1790611200000"},
                    order_index=0,
                    created_by_user_id=user_id,
                ),
                TableRecord(
                    id="pg-epoch-iso",
                    table_id=table_id,
                    data={
                        "title": "ISO overdue",
                        "status": "todo",
                        "due": (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
                    },
                    order_index=1,
                    created_by_user_id=user_id,
                ),
            ]
        )
        await session.commit()

        try:
            payload = await build_my_work(
                session,
                user_id=user_id,
                sections=["projects", "due"],
                timezone_name="UTC",
                limit=20,
            )
            assert payload["sections"]["projects"]["status"] == "ok", payload["sections"]["projects"]
            project = payload["sections"]["projects"]["data"]["items"][0]
            assert project["totalTasks"] == 2
            assert project["overdueCount"] == 1
            assert payload["sections"]["due"]["status"] == "ok"
        finally:
            await session.execute(text("DELETE FROM table_records WHERE table_id = :tid"), {"tid": table_id})
            await session.execute(text("DELETE FROM table_task_profiles WHERE table_id = :tid"), {"tid": table_id})
            await session.execute(text("DELETE FROM table_fields WHERE table_id = :tid"), {"tid": table_id})
            await session.execute(text("DELETE FROM table_views WHERE table_id = :tid"), {"tid": table_id})
            await session.execute(text("DELETE FROM workspace_items WHERE id = :id OR parent_id = :id"), {"id": table_id})
            await session.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id})
            await session.execute(text("DELETE FROM workspace_members WHERE workspace_id = :id"), {"id": workspace_id})
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await session.commit()
