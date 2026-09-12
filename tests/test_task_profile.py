from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.core.config import settings
from app.db.base import Base
from app.models.change_history import ChangeItem, ChangeSet
from app.models.smart_table import TableField, TableView, WorkspaceItem
from app.models.task_profile import TableTaskProfile
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.task_profile import (
    TaskProfileValidationError,
    get_task_profile_record,
    save_task_profile,
    serialize_task_profile,
    suggest_task_profile,
)
from app.services.task_profile_portable import sync_profile_annotations
from app.services.workspace import copy_table_db, create_table_db


PROFILE = {
    "schemaVersion": 1,
    "titleFieldId": "title",
    "statusFieldId": "status",
    "assigneeFieldId": "owner",
    "priorityFieldId": "priority",
    "startDateFieldId": "start",
    "dueDateFieldId": "due",
    "progressFieldId": "progress",
    "parentFieldId": None,
    "dependencyFieldId": None,
    "workloadFieldId": "workload",
    "completedStatusValues": ["done"],
    "blockedStatusValues": ["blocked"],
    "notStartedStatusValues": ["todo"],
}


@pytest_asyncio.fixture
async def profile_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'task-profile.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                User(id=1, email="owner@example.test", password_hash="x", name="Owner"),
                User(id=2, email="viewer@example.test", password_hash="x", name="Viewer"),
                Workspace(id="ws-profile", name="Profiles"),
                WorkspaceItem(
                    id="root-profile",
                    workspace_id="ws-profile",
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="tasks",
                    workspace_id="ws-profile",
                    type="table",
                    name="Tasks",
                    parent_id="root-profile",
                    order_index=1,
                    default_view_id="v1",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(user_id=1, workspace_id="ws-profile", role=WorkspaceRole.owner),
                WorkspaceMember(user_id=2, workspace_id="ws-profile", role=WorkspaceRole.viewer),
            ]
        )
        fields = [
            ("title", "任务名称", "text", None, None),
            (
                "status",
                "状态",
                "select",
                [
                    {"id": "todo", "label": "待开始"},
                    {"id": "doing", "label": "进行中"},
                    {"id": "blocked", "label": "阻塞"},
                    {"id": "done", "label": "已完成"},
                ],
                None,
            ),
            ("owner", "负责人", "member", None, {"multiple": False}),
            ("priority", "优先级", "select", [{"id": "high", "label": "高"}], None),
            ("start", "开始时间", "date", None, None),
            ("due", "截止日期", "date", None, None),
            ("progress", "进度", "progress", None, {"max": 100}),
            ("workload", "工作量", "number", None, None),
        ]
        for index, (field_id, name, field_type, options, prop) in enumerate(fields):
            session.add(
                TableField(
                    id=field_id,
                    table_id="tasks",
                    name=name,
                    type=field_type,
                    options=options,
                    property=prop,
                    order_index=index,
                )
            )
        session.add(
            TableView(
                id="v1",
                table_id="tasks",
                name="Grid",
                type="grid",
                config={},
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_profile_is_field_id_based_and_deleted_field_becomes_invalid(profile_db):
    saved = await save_task_profile(
        profile_db,
        table_id="tasks",
        config=PROFILE,
        user_id=1,
    )
    assert saved["valid"] is True
    assert saved["config"]["titleFieldId"] == "title"

    title = (
        await profile_db.execute(
            select(TableField).where(
                TableField.table_id == "tasks",
                TableField.id == "title",
            )
        )
    ).scalars().one()
    title.name = "完全不同的标题名称"
    await profile_db.commit()

    renamed = await serialize_task_profile(profile_db, "tasks")
    assert renamed is not None
    assert renamed["valid"] is True
    assert renamed["config"]["titleFieldId"] == "title"

    await profile_db.execute(
        delete(TableField).where(
            TableField.table_id == "tasks",
            TableField.id == "due",
        )
    )
    await profile_db.commit()

    invalid = await serialize_task_profile(profile_db, "tasks")
    assert invalid is not None
    assert invalid["valid"] is False
    assert invalid["config"]["dueDateFieldId"] == "due"
    assert any(
        issue["key"] == "dueDateFieldId" and issue["code"] == "FIELD_MISSING"
        for issue in invalid["issues"]
    )


@pytest.mark.asyncio
async def test_profile_rejects_wrong_type_and_unknown_status(profile_db):
    bad_type = dict(PROFILE)
    bad_type["dueDateFieldId"] = "title"
    with pytest.raises(TaskProfileValidationError, match="dueDateFieldId"):
        await save_task_profile(
            profile_db,
            table_id="tasks",
            config=bad_type,
            user_id=1,
        )

    bad_status = dict(PROFILE)
    bad_status["completedStatusValues"] = ["not-an-option"]
    with pytest.raises(TaskProfileValidationError, match="Unknown status values"):
        await save_task_profile(
            profile_db,
            table_id="tasks",
            config=bad_status,
            user_id=1,
        )

    overlapping = dict(PROFILE)
    overlapping["notStartedStatusValues"] = ["done"]
    with pytest.raises(TaskProfileValidationError, match="cannot belong to both"):
        await save_task_profile(
            profile_db,
            table_id="tasks",
            config=overlapping,
            user_id=1,
        )


@pytest.mark.asyncio
async def test_suggestion_handles_chinese_fields_but_never_persists(profile_db):
    suggestion = await suggest_task_profile(profile_db, "tasks")
    assert suggestion["requiresConfirmation"] is True
    assert suggestion["config"]["titleFieldId"] == "title"
    assert suggestion["config"]["statusFieldId"] == "status"
    assert suggestion["config"]["assigneeFieldId"] == "owner"
    assert suggestion["config"]["dueDateFieldId"] == "due"
    assert suggestion["config"]["completedStatusValues"] == ["done"]
    assert suggestion["config"]["blockedStatusValues"] == ["blocked"]
    assert suggestion["config"]["notStartedStatusValues"] == ["todo"]
    assert await get_task_profile_record(profile_db, "tasks") is None


@pytest.mark.asyncio
async def test_graphql_permissions_and_profile_audit(profile_db):
    owner = (
        await profile_db.execute(select(User).where(User.id == 1))
    ).scalars().one()
    viewer = (
        await profile_db.execute(select(User).where(User.id == 2))
    ).scalars().one()

    mutation = """
      mutation UpdateTaskProfile($tableId: String!, $profile: JSON!) {
        updateTaskProfile(tableId: $tableId, profile: $profile)
      }
    """
    owner_result = await schema.execute(
        mutation,
        variable_values={"tableId": "tasks", "profile": PROFILE},
        context_value={"db": profile_db, "user": owner},
    )
    assert owner_result.errors is None
    assert owner_result.data["updateTaskProfile"]["valid"] is True

    viewer_result = await schema.execute(
        mutation,
        variable_values={"tableId": "tasks", "profile": PROFILE},
        context_value={"db": profile_db, "user": viewer},
    )
    assert viewer_result.errors
    assert "No access" in str(viewer_result.errors[0])

    query_result = await schema.execute(
        "query($tableId: String!) { taskProfile(tableId: $tableId) }",
        variable_values={"tableId": "tasks"},
        context_value={"db": profile_db, "user": viewer},
    )
    assert query_result.errors is None
    assert query_result.data["taskProfile"]["config"]["titleFieldId"] == "title"

    change_set = (
        await profile_db.execute(
            select(ChangeSet).where(ChangeSet.operation == "update_task_profile")
        )
    ).scalars().one()
    change_item = (
        await profile_db.execute(
            select(ChangeItem).where(ChangeItem.change_set_id == change_set.id)
        )
    ).scalars().one()
    assert change_item.entity_type == "task_profile"
    assert change_item.entity_id == "tasks"


@pytest.mark.asyncio
async def test_project_template_and_table_copy_materialize_profile(profile_db):
    created = await create_table_db(
        profile_db,
        "From Project Template",
        "root-profile",
        "v1",
        "ws-profile",
        "project_management",
        1,
    )
    template_profile = await serialize_task_profile(profile_db, created["id"])
    assert template_profile is not None
    assert template_profile["valid"] is True
    assert template_profile["config"]["titleFieldId"] == "f1"
    assert template_profile["config"]["statusFieldId"] == "f2"
    assert template_profile["config"]["completedStatusValues"] == ["opt3"]
    assert template_profile["config"]["notStartedStatusValues"] == ["opt1"]

    current = await save_task_profile(
        profile_db,
        table_id="tasks",
        config=PROFILE,
        user_id=1,
        commit=False,
    )
    await sync_profile_annotations(
        profile_db,
        table_id="tasks",
        config=current["config"],
    )
    await profile_db.commit()

    copied = await copy_table_db(
        profile_db,
        "tasks",
        "root-profile",
        "Copied Tasks",
        "ws-profile",
    )
    assert copied is not None
    copied_profile = await serialize_task_profile(profile_db, copied["id"])
    assert copied_profile is not None
    assert copied_profile["valid"] is True
    assert copied_profile["config"]["titleFieldId"] == "title"
    assert copied_profile["config"]["dueDateFieldId"] == "due"
    assert copied_profile["config"]["notStartedStatusValues"] == ["todo"]


@pytest.mark.asyncio
async def test_copy_retargets_semantic_self_relations(profile_db):
    profile_db.add_all(
        [
            TableField(
                id="parent",
                table_id="tasks",
                name="父任务",
                type="relation",
                property={"targetTableId": "tasks", "multiple": False},
                order_index=8,
            ),
            TableField(
                id="dependency",
                table_id="tasks",
                name="依赖",
                type="relation",
                property={"targetTableId": "tasks", "multiple": True},
                order_index=9,
            ),
        ]
    )
    await profile_db.commit()

    relation_profile = dict(PROFILE)
    relation_profile["parentFieldId"] = "parent"
    relation_profile["dependencyFieldId"] = "dependency"
    current = await save_task_profile(
        profile_db,
        table_id="tasks",
        config=relation_profile,
        user_id=1,
        commit=False,
    )
    await sync_profile_annotations(
        profile_db,
        table_id="tasks",
        config=current["config"],
    )
    await profile_db.commit()

    copied = await copy_table_db(
        profile_db,
        "tasks",
        "root-profile",
        "Copied Relations",
        "ws-profile",
    )
    assert copied is not None
    copied_id = str(copied["id"])
    copied_profile = await serialize_task_profile(profile_db, copied_id)
    assert copied_profile is not None
    assert copied_profile["valid"] is True
    assert copied_profile["config"]["parentFieldId"] == "parent"
    assert copied_profile["config"]["dependencyFieldId"] == "dependency"

    copied_relations = list(
        (
            await profile_db.execute(
                select(TableField).where(
                    TableField.table_id == copied_id,
                    TableField.id.in_(["parent", "dependency"]),
                )
            )
        ).scalars().all()
    )
    assert {field.property["targetTableId"] for field in copied_relations} == {copied_id}


@pytest.mark.asyncio
async def test_generic_table_without_semantics_has_null_profile(profile_db):
    blank = await create_table_db(
        profile_db,
        "Blank",
        "root-profile",
        "v1",
        "ws-profile",
        "blank",
        1,
    )
    assert await serialize_task_profile(profile_db, blank["id"]) is None
    assert (
        await profile_db.execute(
            select(TableTaskProfile).where(TableTaskProfile.table_id == blank["id"])
        )
    ).scalars().first() is None
