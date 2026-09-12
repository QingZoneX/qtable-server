from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.models.change_history import ChangeItem, ChangeSet
from app.models.project_steward import ProjectStewardDiagnosis
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    WorkspaceItem,
)
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.schemas.project_steward import ProjectStewardRequest
from app.services.project_steward import project_steward_service


@pytest.fixture
async def steward_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'project-steward.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        today = datetime.now(timezone.utc).date()
        db.add_all(
            [
                User(
                    id=1,
                    email="alice@example.test",
                    password_hash="test",
                    name="Alice",
                ),
                User(
                    id=2,
                    email="bob@example.test",
                    password_hash="test",
                    name="Bob",
                ),
                Workspace(id="w1", name="Workspace One"),
                WorkspaceMember(
                    user_id=1,
                    workspace_id="w1",
                    role=WorkspaceRole.owner,
                ),
                WorkspaceMember(
                    user_id=2,
                    workspace_id="w1",
                    role=WorkspaceRole.editor,
                ),
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
                    default_view_id="v1",
                ),
                TableField(
                    id="title",
                    table_id="tasks",
                    name="任务名称",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id="tasks",
                    name="状态",
                    type="select",
                    options=[
                        {"id": "todo", "label": "待处理"},
                        {"id": "doing", "label": "进行中"},
                        {"id": "done", "label": "已完成"},
                        {"id": "blocked", "label": "阻塞"},
                    ],
                    order_index=1,
                ),
                TableField(
                    id="due",
                    table_id="tasks",
                    name="截止日期",
                    type="date",
                    order_index=2,
                ),
                TableField(
                    id="owner",
                    table_id="tasks",
                    name="负责人",
                    type="member",
                    order_index=3,
                ),
                TableField(
                    id="dep",
                    table_id="tasks",
                    name="前置依赖",
                    type="relation",
                    property={
                        "targetTableId": "tasks",
                        "displayFieldId": "title",
                        "multiple": True,
                    },
                    order_index=4,
                ),
                TableField(
                    id="p50",
                    table_id="tasks",
                    name="P50 工时",
                    type="number",
                    order_index=5,
                ),
                TableField(
                    id="actual",
                    table_id="tasks",
                    name="实际工时",
                    type="number",
                    order_index=6,
                ),
                TableRecord(
                    id="t1",
                    table_id="tasks",
                    data={
                        "title": "登录重构",
                        "status": "doing",
                        "due": (today - timedelta(days=2)).isoformat(),
                        "owner": [{"id": "1", "name": "Alice"}],
                        "dep": [],
                        "p50": 10,
                        "actual": 18,
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=3,
                ),
                TableRecord(
                    id="t2",
                    table_id="tasks",
                    data={
                        "title": "登录测试",
                        "status": "todo",
                        "due": (today + timedelta(days=1)).isoformat(),
                        "owner": [{"id": "2", "name": "Bob"}],
                        "dep": ["t1"],
                        "p50": 8,
                        "actual": None,
                    },
                    order_index=2,
                    created_by_user_id=2,
                    version=2,
                ),
                TableRecord(
                    id="t3",
                    table_id="tasks",
                    data={
                        "title": "上线检查",
                        "status": "todo",
                        "due": (today + timedelta(days=2)).isoformat(),
                        "owner": [],
                        "dep": ["t2"],
                        "p50": 6,
                        "actual": None,
                    },
                    order_index=3,
                    created_by_user_id=1,
                    version=1,
                ),
            ]
        )
        db.add(
            ChangeSet(
                id="cs-stale",
                workspace_id="w1",
                table_id="tasks",
                actor_type="user",
                actor_id=1,
                operation="update_record",
                source="ui",
                summary="status changed",
                created_at=datetime.now(timezone.utc) - timedelta(days=10),
            )
        )
        db.add(
            ChangeItem(
                id="ci-stale",
                change_set_id="cs-stale",
                table_id="tasks",
                entity_type="record",
                entity_id="t1",
                before_data={"status": "todo"},
                after_data={"status": "doing"},
                changed_fields=["status"],
                order_index=0,
            )
        )
        await db.commit()
        yield db

    await engine.dispose()


def request(question: str = "为什么延期，当前阻塞是什么，下一步做什么？"):
    return ProjectStewardRequest(
        workspaceId="w1",
        tableIds=["tasks"],
        question=question,
        timezone="UTC",
        dueSoonDays=3,
        staleTaskDays=7,
        overloadHours=40,
        overloadWindowDays=7,
    )


@pytest.mark.asyncio
async def test_answers_have_traceable_fact_inference_and_suggestion(steward_db):
    result = await project_steward_service.ask(
        steward_db,
        user_id=1,
        request=request(),
    )
    categories = {
        item["category"]
        for item in result["facts"] + result["inferences"]
    }
    assert {"overdue", "estimate_variance", "blocked"} <= categories
    assert result["suggestions"]
    assert result["readOnly"] is True
    assert result["snapshot"]["visibleRecordCount"] == 3
    assert result["snapshot"]["isStale"] is False
    assert "事实：" in result["answer"]
    assert "推断：" in result["answer"]
    assert "建议：" in result["answer"]

    evidence = [
        evidence
        for item in result["facts"] + result["inferences"] + result["suggestions"]
        for evidence in item["evidence"]
    ]
    assert any(item["recordId"] == "t1" for item in evidence)
    assert all(
        item["deepLink"].startswith("/workbench/tasks/v1?recordId=")
        for item in evidence
    )
    assert await steward_db.get(
        ProjectStewardDiagnosis,
        result["diagnosisId"],
    ) is not None


@pytest.mark.asyncio
async def test_multi_table_context_does_not_treat_project_rows_as_unassigned_tasks(steward_db):
    steward_db.add(
        WorkspaceItem(
            id="projects",
            workspace_id="w1",
            type="table",
            name="Projects",
            parent_id="root",
            order_index=2,
            default_view_id="pv1",
        )
    )
    steward_db.add(
        TableField(
            id="project-title",
            table_id="projects",
            name="项目名称",
            type="text",
            order_index=0,
        )
    )
    steward_db.add(
        TableField(
            id="project-status",
            table_id="projects",
            name="状态",
            type="select",
            options=[
                {"id": "doing", "label": "进行中"},
                {"id": "done", "label": "已完成"},
            ],
            order_index=1,
        )
    )
    steward_db.add(
        TableRecord(
            id="p1",
            table_id="projects",
            data={"project-title": "QTable 项目", "project-status": "doing"},
            order_index=1,
            created_by_user_id=1,
            version=1,
        )
    )
    await steward_db.commit()

    multi_table_request = ProjectStewardRequest(
        workspaceId="w1",
        tableIds=["projects", "tasks"],
        question="当前项目有什么风险？",
        timezone="UTC",
    )
    result = await project_steward_service.ask(
        steward_db,
        user_id=1,
        request=multi_table_request,
    )

    assert result["snapshot"]["visibleRecordCount"] == 4
    # Only task t3 is actually unassigned. The project row has no owner field,
    # so it must not be turned into a fake "unassigned task" diagnosis.
    assert result["diagnostics"]["unassignedCount"] == 1


@pytest.mark.asyncio
async def test_row_permissions_prevent_hidden_counts_relations_and_evidence(steward_db):
    steward_db.add(
        TableRowPermissionPolicy(
            table_id="tasks",
            mode="creator",
            member_field_id=None,
        )
    )
    await steward_db.commit()

    result = await project_steward_service.ask(
        steward_db,
        user_id=2,
        request=request("当前阻塞是什么？"),
    )
    assert result["snapshot"]["visibleRecordCount"] == 1
    evidence = [
        item
        for conclusion in (
            result["facts"] + result["inferences"] + result["suggestions"]
        )
        for item in conclusion["evidence"]
    ]
    assert {item["recordId"] for item in evidence} <= {"t2"}
    assert all(item["title"] == "登录测试" for item in evidence)
    assert result["diagnostics"]["blockedCount"] == 0


@pytest.mark.asyncio
async def test_snapshot_change_marks_old_result_stale_and_redacts_details(steward_db):
    result = await project_steward_service.ask(
        steward_db,
        user_id=1,
        request=request(),
    )
    fresh = await project_steward_service.get_diagnosis(
        steward_db,
        user_id=1,
        diagnosis_id=result["diagnosisId"],
    )
    assert fresh["isStale"] is False
    assert fresh["result"] is not None

    record = await steward_db.get(
        TableRecord,
        {"id": "t2", "table_id": "tasks"},
    )
    record.data = {**record.data, "status": "done"}
    record.version += 1
    await steward_db.commit()

    stale = await project_steward_service.get_diagnosis(
        steward_db,
        user_id=1,
        diagnosis_id=result["diagnosisId"],
    )
    assert stale["isStale"] is True
    assert stale["result"] is None
    assert "changed" in stale["staleReason"].lower()


@pytest.mark.asyncio
async def test_steward_does_not_modify_qtable_business_data(steward_db):
    fields_before = (
        await steward_db.execute(
            select(func.count())
            .select_from(TableField)
            .where(TableField.table_id == "tasks")
        )
    ).scalar_one()
    versions_before = {
        row.id: row.version
        for row in (
            await steward_db.execute(
                select(TableRecord).where(TableRecord.table_id == "tasks")
            )
        ).scalars().all()
    }
    changes_before = (
        await steward_db.execute(
            select(func.count())
            .select_from(ChangeSet)
            .where(ChangeSet.table_id == "tasks")
        )
    ).scalar_one()

    await project_steward_service.ask(
        steward_db,
        user_id=1,
        request=request("今天最应该推进什么？"),
    )

    fields_after = (
        await steward_db.execute(
            select(func.count())
            .select_from(TableField)
            .where(TableField.table_id == "tasks")
        )
    ).scalar_one()
    versions_after = {
        row.id: row.version
        for row in (
            await steward_db.execute(
                select(TableRecord).where(TableRecord.table_id == "tasks")
            )
        ).scalars().all()
    }
    changes_after = (
        await steward_db.execute(
            select(func.count())
            .select_from(ChangeSet)
            .where(ChangeSet.table_id == "tasks")
        )
    ).scalar_one()

    assert fields_after == fields_before
    assert versions_after == versions_before
    assert changes_after == changes_before


@pytest.mark.asyncio
async def test_missing_ai_config_uses_safe_deterministic_fallback(steward_db):
    result = await project_steward_service.ask(
        steward_db,
        user_id=1,
        request=request(),
    )
    assert result["provider"] == "deterministic-fallback"
    assert result["answer"]


def test_graphql_schema_exposes_project_steward_contract():
    schema_text = schema.as_str()
    assert "projectStewardAsk(" in schema_text
    assert "projectStewardDiagnosis(" in schema_text
