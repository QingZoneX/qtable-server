from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.core.config import settings
from app.db.base import Base
from app.models.change_history import ChangeItem
from app.models.my_work import MyWorkRecentTarget
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
from app.services.change_history import append_change_set
from app.services.my_work import (
    MyWorkValidationError,
    build_my_work,
    import_recent_targets,
    upsert_recent_target,
)
from app.services.task_profile import suggest_task_profile_config


ALICE = 11
BOB = 12

PROFILE = {
    "schemaVersion": 1,
    "titleFieldId": "title",
    "statusFieldId": "status",
    "assigneeFieldId": "owner",
    "priorityFieldId": "priority",
    "startDateFieldId": None,
    "dueDateFieldId": "due",
    "progressFieldId": "progress",
    "parentFieldId": None,
    "dependencyFieldId": None,
    "workloadFieldId": None,
    "completedStatusValues": ["done"],
    "blockedStatusValues": ["blocked"],
    "notStartedStatusValues": ["todo"],
}


@pytest_asyncio.fixture
async def my_work_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'my-work.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        today = datetime.now(timezone.utc).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        today_text = today.isoformat()
        tomorrow = (today + timedelta(days=1)).isoformat()

        alice = User(
            id=ALICE,
            email="alice-my-work@example.test",
            name="Alice",
            password_hash="!test",
        )
        bob = User(
            id=BOB,
            email="bob-my-work@example.test",
            name="Bob",
            password_hash="!test",
        )
        workspace = Workspace(id="ws-my-work", name="My Work")
        root = WorkspaceItem(
            id="root-my-work",
            workspace_id=workspace.id,
            type="folder",
            name="Root",
            parent_id=None,
            order_index=0,
        )
        tasks = WorkspaceItem(
            id="tasks-my-work",
            workspace_id=workspace.id,
            type="table",
            name="Delivery Tasks",
            parent_id=root.id,
            order_index=1,
            default_view_id="v1",
        )
        generic = WorkspaceItem(
            id="generic-my-work",
            workspace_id=workspace.id,
            type="table",
            name="Generic Data",
            parent_id=root.id,
            order_index=2,
            default_view_id="v1",
        )
        session.add_all([alice, bob, workspace, root, tasks, generic])
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=ALICE,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=BOB,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.editor,
                ),
            ]
        )

        status_options = [
            {"id": "todo", "label": "待开始"},
            {"id": "doing", "label": "进行中"},
            {"id": "blocked", "label": "阻塞"},
            {"id": "done", "label": "已完成"},
        ]
        session.add_all(
            [
                TableField(
                    id="title",
                    table_id=tasks.id,
                    name="任务名称",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id=tasks.id,
                    name="状态",
                    type="select",
                    options=status_options,
                    order_index=1,
                ),
                TableField(
                    id="owner",
                    table_id=tasks.id,
                    name="负责人",
                    type="member",
                    property={"multiple": True},
                    order_index=2,
                ),
                TableField(
                    id="priority",
                    table_id=tasks.id,
                    name="优先级",
                    type="select",
                    options=[
                        {"id": "high", "label": "高"},
                        {"id": "low", "label": "低"},
                    ],
                    order_index=3,
                ),
                TableField(
                    id="due",
                    table_id=tasks.id,
                    name="截止日期",
                    type="date",
                    order_index=4,
                ),
                TableField(
                    id="progress",
                    table_id=tasks.id,
                    name="进度",
                    type="progress",
                    property={"max": 100},
                    order_index=5,
                ),
                TableView(
                    id="v1",
                    table_id=tasks.id,
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableField(
                    id="generic-title",
                    table_id=generic.id,
                    name="名称",
                    type="text",
                    order_index=0,
                ),
                TableView(
                    id="v1",
                    table_id=generic.id,
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableTaskProfile(
                    table_id=tasks.id,
                    config=dict(PROFILE),
                    updated_by_user_id=ALICE,
                ),
                TableRowPermissionPolicy(
                    table_id=tasks.id,
                    mode="member_field",
                    member_field_id="owner",
                ),
            ]
        )

        records = [
            TableRecord(
                id="alice-overdue",
                table_id=tasks.id,
                data={
                    "title": "Overdue",
                    "status": "doing",
                    "owner": [str(ALICE)],
                    "priority": "high",
                    "due": yesterday,
                    "progress": 20,
                },
                order_index=0,
                created_by_user_id=BOB,
            ),
            TableRecord(
                id="alice-today",
                table_id=tasks.id,
                data={
                    "title": "Today",
                    "status": "todo",
                    "owner": [str(ALICE)],
                    "priority": "low",
                    "due": today_text,
                    "progress": 0,
                },
                order_index=1,
                created_by_user_id=BOB,
            ),
            TableRecord(
                id="alice-done",
                table_id=tasks.id,
                data={
                    "title": "Done",
                    "status": "done",
                    "owner": [str(ALICE)],
                    "priority": "low",
                    "due": yesterday,
                    "progress": 100,
                },
                order_index=2,
                created_by_user_id=BOB,
            ),
            TableRecord(
                id="alice-blocked",
                table_id=tasks.id,
                data={
                    "title": "Blocked",
                    "status": "blocked",
                    "owner": [str(ALICE)],
                    "priority": "high",
                    "due": tomorrow,
                    "progress": 30,
                },
                order_index=3,
                created_by_user_id=BOB,
            ),
            TableRecord(
                id="bob-hidden",
                table_id=tasks.id,
                data={
                    "title": "Bob Secret",
                    "status": "doing",
                    "owner": [str(BOB)],
                    "priority": "high",
                    "due": yesterday,
                    "progress": 50,
                },
                order_index=4,
                created_by_user_id=BOB,
            ),
            TableRecord(
                id="generic-row",
                table_id=generic.id,
                data={"generic-title": "Generic visible activity"},
                order_index=0,
                created_by_user_id=ALICE,
            ),
        ]
        session.add_all(records)
        await session.commit()

        await append_change_set(
            session,
            table_id=tasks.id,
            actor_id=ALICE,
            operation="update",
            summary="Completed task",
            items=[
                {
                    "entity_type": "record",
                    "entity_id": "alice-done",
                    "before_data": {
                        "title": "Done",
                        "status": "doing",
                        "owner": [str(ALICE)],
                    },
                    "after_data": {
                        "title": "Done",
                        "status": "done",
                        "owner": [str(ALICE)],
                    },
                    "changed_fields": ["status"],
                }
            ],
        )
        await append_change_set(
            session,
            table_id=tasks.id,
            actor_id=BOB,
            operation="update",
            summary="Hidden Bob change",
            items=[
                {
                    "entity_type": "record",
                    "entity_id": "bob-hidden",
                    "before_data": {
                        "title": "Bob Secret",
                        "status": "todo",
                        "owner": [str(BOB)],
                    },
                    "after_data": {
                        "title": "Bob Secret",
                        "status": "doing",
                        "owner": [str(BOB)],
                    },
                    "changed_fields": ["status"],
                }
            ],
        )
        await append_change_set(
            session,
            table_id=generic.id,
            actor_id=ALICE,
            operation="create",
            summary="Created generic row",
            items=[
                {
                    "entity_type": "record",
                    "entity_id": "generic-row",
                    "before_data": None,
                    "after_data": {"generic-title": "Generic visible activity"},
                }
            ],
        )
        await session.commit()

        yield session, alice, bob, tasks, generic

    await engine.dispose()


def _data(payload, section):
    return payload["sections"][section]["data"]


@pytest.mark.asyncio
async def test_my_work_tasks_states_sort_and_row_security(my_work_db):
    session, alice, _, _, _ = my_work_db

    all_payload = await build_my_work(
        session,
        user_id=alice.id,
        sections=["tasks"],
        timezone_name="UTC",
        task_state="all",
        limit=20,
    )
    tasks = _data(all_payload, "tasks")
    assert tasks["totalCount"] == 4
    assert [item["recordId"] for item in tasks["items"]] == [
        "alice-overdue",
        "alice-today",
        "alice-blocked",
        "alice-done",
    ]
    assert all(item["recordId"] != "bob-hidden" for item in tasks["items"])
    assert tasks["items"][0]["timingBucket"] == "overdue"
    assert tasks["items"][-1]["timingBucket"] == "completed"

    expected = {
        "in_progress": {"alice-overdue", "alice-blocked"},
        "not_started": {"alice-today"},
        "completed": {"alice-done"},
    }
    for state, record_ids in expected.items():
        payload = await build_my_work(
            session,
            user_id=alice.id,
            sections=["tasks"],
            timezone_name="UTC",
            task_state=state,
            limit=20,
        )
        returned = {
            item["recordId"]
            for item in _data(payload, "tasks")["items"]
        }
        assert returned == record_ids


@pytest.mark.asyncio
async def test_due_windows_are_exact_include_not_started_and_deep_link(my_work_db):
    session, alice, _, tasks, _ = my_work_db
    payload = await build_my_work(
        session,
        user_id=alice.id,
        sections=["due"],
        timezone_name="UTC",
        limit=20,
    )
    due = _data(payload, "due")

    assert due["overdue"]["totalCount"] == 1
    assert due["overdue"]["items"][0]["recordId"] == "alice-overdue"
    assert due["today"]["totalCount"] == 1
    assert due["today"]["items"][0]["recordId"] == "alice-today"
    assert due["today"]["items"][0]["isNotStarted"] is True
    assert due["next7d"]["totalCount"] >= 1
    assert any(
        item["recordId"] == "alice-blocked"
        for item in due["next7d"]["items"]
    )
    assert due["overdue"]["items"][0]["deepLink"] == (
        f"/workbench/{tasks.id}/v1?recordId=alice-overdue"
    )
    for bucket in ("overdue", "today", "next24h", "next3d", "next7d"):
        assert all(
            item["recordId"] != "bob-hidden"
            for item in due[bucket]["items"]
        )


@pytest.mark.asyncio
async def test_project_aggregate_and_kpi_do_not_leak_hidden_rows(my_work_db):
    session, alice, _, tasks, _ = my_work_db
    payload = await build_my_work(
        session,
        user_id=alice.id,
        sections=["projects", "kpi"],
        timezone_name="UTC",
        limit=20,
    )
    projects = _data(payload, "projects")
    project = next(
        item for item in projects["items"]
        if item["tableId"] == tasks.id
    )
    assert project["totalTasks"] == 4
    assert project["completedTasks"] == 1
    assert project["incompleteTasks"] == 3
    assert project["overdueCount"] == 1
    assert project["blockedCount"] == 1
    assert project["progress"] == 37.5

    kpi = _data(payload, "kpi")
    assert kpi["myIncompleteCount"] == 3
    assert kpi["completedThisWeekCount"] == 1
    assert kpi["activeProjectCount"] == 1
    assert kpi["overdueOrRiskCount"] == 2


@pytest.mark.asyncio
async def test_activity_uses_current_row_security_and_includes_generic_tables(my_work_db):
    session, alice, _, _, _ = my_work_db
    payload = await build_my_work(
        session,
        user_id=alice.id,
        sections=["activity"],
        timezone_name="UTC",
        limit=50,
    )
    activity = _data(payload, "activity")["items"]

    ids = {item["entity"]["id"] for item in activity}
    assert "alice-done" in ids
    assert "generic-row" in ids
    assert "bob-hidden" not in ids

    completed = next(
        item for item in activity
        if item["entity"]["id"] == "alice-done"
    )
    assert "task.status_changed" in completed["kinds"]
    assert completed["deepLink"].endswith("?recordId=alice-done")

    generic = next(
        item for item in activity
        if item["entity"]["id"] == "generic-row"
    )
    assert generic["kinds"] == ["record.created"]


@pytest.mark.asyncio
async def test_recent_targets_import_local_contract_and_trim_after_permission_loss(my_work_db):
    session, alice, _, tasks, _ = my_work_db
    result = await import_recent_targets(
        session,
        user_id=alice.id,
        raw_targets=[
            {
                "entityType": "table",
                "entityId": tasks.id,
                "title": "Untrusted local title",
                "deepLink": "/wrong",
                "table": {
                    "id": tasks.id,
                    "name": "Untrusted table",
                    "defaultViewId": "v1",
                },
                "visitedAt": 1_700_000_000_000,
            },
            {
                "entityType": "record",
                "entityId": "alice-overdue",
                "tableId": tasks.id,
                "title": "Untrusted record title",
                "deepLink": "/wrong-record",
                "visitedAt": 1_700_000_000_100,
            },
            {
                "entityType": "record",
                "entityId": "bob-hidden",
                "tableId": tasks.id,
                "visitedAt": 1_700_000_000_200,
            },
        ],
    )
    assert result["importedCount"] == 2
    assert result["skippedCount"] == 1

    payload = await build_my_work(
        session,
        user_id=alice.id,
        sections=["recent"],
        timezone_name="UTC",
        limit=20,
    )
    recent = _data(payload, "recent")["items"]
    record_target = next(
        item for item in recent
        if item["entityType"] == "record"
    )
    assert record_target["title"] == "Overdue"
    assert record_target["deepLink"].endswith(
        "?recordId=alice-overdue"
    )

    record = (
        await session.execute(
            select(TableRecord).where(
                TableRecord.table_id == tasks.id,
                TableRecord.id == "alice-overdue",
            )
        )
    ).scalars().one()
    record.data = {
        **dict(record.data),
        "owner": [str(BOB)],
    }
    await session.commit()

    trimmed = await build_my_work(
        session,
        user_id=alice.id,
        sections=["recent"],
        timezone_name="UTC",
        limit=20,
    )
    assert all(
        item["entityId"] != "alice-overdue"
        for item in _data(trimmed, "recent")["items"]
    )


@pytest.mark.asyncio
async def test_recent_table_disappears_immediately_after_workspace_access_loss(my_work_db):
    session, alice, _, tasks, _ = my_work_db
    await upsert_recent_target(
        session,
        user_id=alice.id,
        raw_target={
            "entityType": "table",
            "entityId": tasks.id,
            "visitedAt": datetime.now(timezone.utc).isoformat(),
        },
    )
    await session.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.user_id == alice.id,
            WorkspaceMember.workspace_id == tasks.workspace_id,
        )
    )
    await session.commit()

    payload = await build_my_work(
        session,
        user_id=alice.id,
        sections=["recent"],
        timezone_name="UTC",
        limit=20,
    )
    assert _data(payload, "recent")["items"] == []


@pytest.mark.asyncio
async def test_section_failure_isolated_and_cursor_pages(monkeypatch, my_work_db):
    session, alice, _, _, _ = my_work_db

    first = await build_my_work(
        session,
        user_id=alice.id,
        sections=["tasks"],
        timezone_name="UTC",
        task_state="in_progress",
        limit=1,
    )
    first_tasks = _data(first, "tasks")
    assert len(first_tasks["items"]) == 1
    cursor = first_tasks["pageInfo"]["nextCursor"]
    assert cursor

    second = await build_my_work(
        session,
        user_id=alice.id,
        sections=["tasks"],
        timezone_name="UTC",
        task_state="in_progress",
        limit=1,
        cursors={"tasks": cursor},
    )
    assert (
        _data(second, "tasks")["items"][0]["recordId"]
        != first_tasks["items"][0]["recordId"]
    )

    import app.services.my_work as my_work_service

    async def fail_projects(*args, **kwargs):
        raise RuntimeError("project source unavailable")

    monkeypatch.setattr(
        my_work_service,
        "_projects_section",
        fail_projects,
    )
    isolated = await build_my_work(
        session,
        user_id=alice.id,
        sections=["tasks", "projects"],
        timezone_name="UTC",
        limit=5,
    )
    assert isolated["sections"]["tasks"]["status"] == "ok"
    assert isolated["sections"]["projects"]["status"] == "error"
    assert (
        isolated["sections"]["projects"]["error"]["code"]
        == "SECTION_FAILED"
    )


@pytest.mark.asyncio
async def test_graphql_contract_auth_timezone_and_performance(my_work_db):
    session, alice, _, _, _ = my_work_db
    query = """
      query MyWork($sections: [String!], $timezone: String!) {
        myWork(sections: $sections, timezone: $timezone, limit: 5)
      }
    """
    result = await schema.execute(
        query,
        variable_values={
            "sections": ["tasks", "kpi"],
            "timezone": "UTC",
        },
        context_value={"db": session, "user": alice},
    )
    assert result.errors is None
    payload = result.data["myWork"]
    assert payload["generatedAt"]
    assert payload["timezone"] == "UTC"
    assert payload["cache"]["mode"] == "none"
    assert payload["performance"]["totalMs"] >= 0

    bad_timezone = await schema.execute(
        query,
        variable_values={
            "sections": ["tasks"],
            "timezone": "Mars/QTable",
        },
        context_value={"db": session, "user": alice},
    )
    assert bad_timezone.errors
    assert "Unknown timezone" in str(bad_timezone.errors[0])


def test_task_profile_suggestion_includes_explicit_not_started_status():
    suggestion = suggest_task_profile_config(
        [
            {"id": "title", "name": "任务名称", "type": "text"},
            {
                "id": "status",
                "name": "状态",
                "type": "select",
                "options": [
                    {"id": "todo", "label": "待开始"},
                    {"id": "doing", "label": "进行中"},
                    {"id": "done", "label": "已完成"},
                ],
            },
        ]
    )
    assert suggestion["config"]["notStartedStatusValues"] == ["todo"]
    assert suggestion["config"]["completedStatusValues"] == ["done"]
