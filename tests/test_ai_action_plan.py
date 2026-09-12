from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.models.ai_action_plan import AiActionPlanBatch
from app.models.change_history import ChangeSet
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    WorkspaceItem,
)
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.schemas.ai_action_plan import (
    AiActionPlanApplyRequest,
    AiActionPlanPreviewRequest,
)
from app.schemas.project_steward import ProjectStewardRequest
from app.services.ai_action_plan import AiActionPlanError, ai_action_plan_service
from app.services.change_history import undo_change_set
from app.services.project_steward import project_steward_service


@pytest.fixture
async def action_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ai-action-plan.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        today = datetime.now(timezone.utc).date()
        db.add_all(
            [
                User(id=1, email="alice@example.test", password_hash="x", name="Alice"),
                User(id=2, email="bob@example.test", password_hash="x", name="Bob"),
                User(id=3, email="carol@example.test", password_hash="x", name="Carol"),
                User(id=4, email="outsider@example.test", password_hash="x", name="Outsider"),
                Workspace(id="w1", name="Workspace"),
                WorkspaceMember(user_id=1, workspace_id="w1", role=WorkspaceRole.owner),
                WorkspaceMember(user_id=2, workspace_id="w1", role=WorkspaceRole.editor),
                WorkspaceMember(user_id=3, workspace_id="w1", role=WorkspaceRole.viewer),
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
                TableField(id="title", table_id="tasks", name="任务名称", type="text", order_index=0),
                TableField(id="desc", table_id="tasks", name="任务描述", type="text", order_index=1),
                TableField(
                    id="status",
                    table_id="tasks",
                    name="状态",
                    type="select",
                    options=[
                        {"id": "todo", "label": "待处理"},
                        {"id": "doing", "label": "进行中"},
                        {"id": "done", "label": "已完成"},
                    ],
                    order_index=2,
                ),
                TableField(
                    id="owner",
                    table_id="tasks",
                    name="负责人",
                    type="member",
                    property={"multiple": True},
                    order_index=3,
                ),
                TableField(
                    id="priority",
                    table_id="tasks",
                    name="优先级",
                    type="select",
                    options=[
                        {"id": "low", "label": "低"},
                        {"id": "medium", "label": "中"},
                        {"id": "high", "label": "高"},
                    ],
                    order_index=4,
                ),
                TableField(id="due", table_id="tasks", name="截止日期", type="date", order_index=5),
                TableField(
                    id="tags",
                    table_id="tasks",
                    name="标签",
                    type="multiSelect",
                    options=[
                        {"id": "backend", "label": "后端"},
                        {"id": "frontend", "label": "前端"},
                        {"id": "urgent", "label": "紧急"},
                    ],
                    order_index=6,
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
                    order_index=7,
                ),
                TableField(id="p50", table_id="tasks", name="P50 工时", type="number", order_index=8),
                TableRecord(
                    id="t1",
                    table_id="tasks",
                    data={
                        "title": "登录重构",
                        "desc": "重构认证链路",
                        "status": "doing",
                        "owner": [{"id": "1", "userId": 1, "name": "Alice"}],
                        "priority": "medium",
                        "due": (today - timedelta(days=2)).isoformat(),
                        "tags": ["backend"],
                        "dep": [],
                        "p50": 12,
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRecord(
                    id="t2",
                    table_id="tasks",
                    data={
                        "title": "登录测试",
                        "desc": "补齐自动化测试",
                        "status": "todo",
                        "owner": [],
                        "priority": "low",
                        "due": (today + timedelta(days=2)).isoformat(),
                        "tags": ["backend"],
                        "dep": ["t1"],
                        "p50": 8,
                    },
                    order_index=2,
                    created_by_user_id=2,
                    version=1,
                ),
            ]
        )
        await db.commit()
        yield db

    await engine.dispose()


def steward_request():
    return ProjectStewardRequest(
        workspaceId="w1",
        tableIds=["tasks"],
        question="为什么延期，哪里有风险，下一步做什么？",
        timezone="UTC",
        dueSoonDays=3,
    )


async def diagnosis_id(db, user_id=1):
    result = await project_steward_service.ask(
        db,
        user_id=user_id,
        request=steward_request(),
    )
    return result["diagnosisId"]


@pytest.mark.asyncio
async def test_diagnosis_generates_safe_fallback_action_plan_without_business_writes(action_db):
    diagnosis = await diagnosis_id(action_db)

    records_before = {
        row.id: (dict(row.data or {}), row.version)
        for row in (
            await action_db.execute(
                select(TableRecord).where(TableRecord.table_id == "tasks")
            )
        ).scalars().all()
    }
    changes_before = (
        await action_db.execute(select(func.count()).select_from(ChangeSet))
    ).scalar_one()

    preview = await ai_action_plan_service.preview(
        action_db,
        user_id=1,
        request=AiActionPlanPreviewRequest.model_validate(
            {"diagnosisId": diagnosis}
        ),
    )

    assert preview["previewOnly"] is True
    assert preview["provider"] == "deterministic-fallback"
    assert preview["actions"]
    assert any(item["type"] == "set_priority" for item in preview["actions"])
    assert all(item["canApply"] is True for item in preview["actions"])

    records_after = {
        row.id: (dict(row.data or {}), row.version)
        for row in (
            await action_db.execute(
                select(TableRecord).where(TableRecord.table_id == "tasks")
            )
        ).scalars().all()
    }
    changes_after = (
        await action_db.execute(select(func.count()).select_from(ChangeSet))
    ).scalar_one()
    assert records_after == records_before
    assert changes_after == changes_before
    assert await action_db.get(AiActionPlanBatch, preview["planId"]) is not None


@pytest.mark.asyncio
async def test_preview_supports_owner_priority_deadline_and_create_task(action_db):
    diagnosis = await diagnosis_id(action_db)
    future = (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()

    preview = await ai_action_plan_service.preview(
        action_db,
        user_id=1,
        request=AiActionPlanPreviewRequest.model_validate(
            {
                "diagnosisId": diagnosis,
                "proposedActions": [
                    {
                        "type": "assign_owner",
                        "tableId": "tasks",
                        "recordId": "t1",
                        "userId": 2,
                        "reason": "让 Bob 接手登录重构。",
                        "confidence": 0.8,
                    },
                    {
                        "type": "set_priority",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": "高",
                        "reason": "临近截止日期。",
                        "confidence": 0.9,
                    },
                    {
                        "type": "set_deadline",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": future,
                        "reason": "根据最新排期调整。",
                        "confidence": 0.7,
                    },
                    {
                        "type": "create_task",
                        "tableId": "tasks",
                        "title": "上线前回归",
                        "description": "执行认证链路完整回归",
                        "userId": 2,
                        "priority": "高",
                        "deadline": future,
                        "status": "待处理",
                        "tags": ["后端", "紧急"],
                        "dependencyRecordIds": ["t2"],
                        "reason": "项目上线前需要独立回归任务。",
                        "confidence": 0.85,
                    },
                ],
            }
        ),
    )

    assert preview["provider"] == "user-edited"
    assert preview["actionCount"] == 4
    actions = {item["type"]: item for item in preview["actions"]}
    assert actions["assign_owner"]["currentDisplayValue"][0]["name"] == "Alice"
    assert actions["assign_owner"]["suggestedDisplayValue"][0]["name"] == "Bob"
    assert actions["set_priority"]["currentDisplayValue"] == "低"
    assert actions["set_priority"]["suggestedDisplayValue"] == "高"
    assert actions["set_deadline"]["suggestedValue"] == future
    create = actions["create_task"]
    assert create["recordId"].startswith("rai_")
    assert create["suggestedValue"]["title"] == "上线前回归"
    assert create["suggestedValue"]["priority"] == "high"
    assert create["suggestedValue"]["status"] == "todo"
    assert create["suggestedValue"]["tags"] == ["backend", "urgent"]
    assert create["suggestedValue"]["dep"] == ["t2"]


@pytest.mark.asyncio
async def test_apply_four_core_actions_is_audited_and_undoable(action_db):
    diagnosis = await diagnosis_id(action_db)
    future = (datetime.now(timezone.utc).date() + timedelta(days=8)).isoformat()
    preview = await ai_action_plan_service.preview(
        action_db,
        user_id=1,
        request=AiActionPlanPreviewRequest.model_validate(
            {
                "diagnosisId": diagnosis,
                "proposedActions": [
                    {
                        "type": "assign_owner",
                        "tableId": "tasks",
                        "recordId": "t1",
                        "userId": 2,
                        "reason": "调整负责人",
                    },
                    {
                        "type": "set_priority",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": "high",
                        "reason": "提高优先级",
                    },
                    {
                        "type": "set_deadline",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": future,
                        "reason": "调整截止日期",
                    },
                    {
                        "type": "create_task",
                        "tableId": "tasks",
                        "title": "发布检查",
                        "priority": "高",
                        "status": "待处理",
                        "reason": "新增发布前检查",
                    },
                ],
            }
        ),
    )
    create_action = next(
        item for item in preview["actions"] if item["type"] == "create_task"
    )

    applied = await ai_action_plan_service.apply(
        action_db,
        user_id=1,
        request=AiActionPlanApplyRequest.model_validate(
            {"planId": preview["planId"]}
        ),
    )

    assert applied["status"] == "applied"
    assert applied["idempotent"] is False
    assert len(applied["appliedActionIds"]) == 4
    assert len(applied["changeSetIds"]) == 1

    t1 = await action_db.get(TableRecord, {"id": "t1", "table_id": "tasks"})
    t2 = await action_db.get(TableRecord, {"id": "t2", "table_id": "tasks"})
    created = await action_db.get(
        TableRecord,
        {"id": create_action["recordId"], "table_id": "tasks"},
    )
    assert t1.data["owner"][0]["id"] == "2"
    assert t2.data["priority"] == "high"
    assert t2.data["due"] == future
    assert created is not None
    assert created.data["title"] == "发布检查"

    change_set = await action_db.get(ChangeSet, applied["changeSetId"])
    assert change_set is not None
    assert change_set.operation == "ai_action_apply"
    assert change_set.actor_type == "ai"
    assert change_set.source == "ai_action_plan"
    assert change_set.trace_id == preview["traceId"]

    await undo_change_set(
        action_db,
        change_set_id=applied["changeSetId"],
        actor_id=1,
        source="test",
    )

    t1_restored = await action_db.get(TableRecord, {"id": "t1", "table_id": "tasks"})
    t2_restored = await action_db.get(TableRecord, {"id": "t2", "table_id": "tasks"})
    created_after_undo = await action_db.get(
        TableRecord,
        {"id": create_action["recordId"], "table_id": "tasks"},
    )
    assert t1_restored.data["owner"][0]["id"] == "1"
    assert t2_restored.data["priority"] == "low"
    assert created_after_undo is None


@pytest.mark.asyncio
async def test_partial_apply_can_continue_on_same_record_without_false_conflict(action_db):
    diagnosis = await diagnosis_id(action_db)
    future = (datetime.now(timezone.utc).date() + timedelta(days=9)).isoformat()
    preview = await ai_action_plan_service.preview(
        action_db,
        user_id=1,
        request=AiActionPlanPreviewRequest.model_validate(
            {
                "diagnosisId": diagnosis,
                "proposedActions": [
                    {
                        "actionId": "priority-action",
                        "type": "set_priority",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": "high",
                        "reason": "先提高优先级",
                    },
                    {
                        "actionId": "deadline-action",
                        "type": "set_deadline",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": future,
                        "reason": "再确认排期",
                    },
                ],
            }
        ),
    )

    first = await ai_action_plan_service.apply(
        action_db,
        user_id=1,
        request=AiActionPlanApplyRequest.model_validate(
            {
                "planId": preview["planId"],
                "actionIds": ["priority-action"],
            }
        ),
    )
    assert first["status"] == "partially_applied"

    after_first = await action_db.get(
        TableRecord, {"id": "t2", "table_id": "tasks"}
    )
    assert after_first.data["priority"] == "high"
    assert after_first.data["due"] != future
    assert after_first.version == 2

    second = await ai_action_plan_service.apply(
        action_db,
        user_id=1,
        request=AiActionPlanApplyRequest.model_validate(
            {
                "planId": preview["planId"],
                "actionIds": ["deadline-action"],
            }
        ),
    )
    assert second["status"] == "applied"
    assert len(second["changeSetIds"]) == 2

    after_second = await action_db.get(
        TableRecord, {"id": "t2", "table_id": "tasks"}
    )
    assert after_second.data["due"] == future
    assert after_second.version == 3


@pytest.mark.asyncio
async def test_repeated_apply_of_same_actions_is_idempotent(action_db):
    diagnosis = await diagnosis_id(action_db)
    preview = await ai_action_plan_service.preview(
        action_db,
        user_id=1,
        request=AiActionPlanPreviewRequest.model_validate(
            {
                "diagnosisId": diagnosis,
                "proposedActions": [
                    {
                        "actionId": "p1",
                        "type": "set_priority",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": "high",
                        "reason": "风险升级",
                    }
                ],
            }
        ),
    )
    request = AiActionPlanApplyRequest.model_validate(
        {"planId": preview["planId"], "actionIds": ["p1"]}
    )
    first = await ai_action_plan_service.apply(action_db, user_id=1, request=request)
    second = await ai_action_plan_service.apply(action_db, user_id=1, request=request)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["changeSetIds"] == first["changeSetIds"]


@pytest.mark.asyncio
async def test_concurrent_record_change_blocks_apply_without_overwrite(action_db):
    diagnosis = await diagnosis_id(action_db)
    preview = await ai_action_plan_service.preview(
        action_db,
        user_id=1,
        request=AiActionPlanPreviewRequest.model_validate(
            {
                "diagnosisId": diagnosis,
                "proposedActions": [
                    {
                        "type": "set_priority",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": "high",
                        "reason": "风险升级",
                    }
                ],
            }
        ),
    )

    record = await action_db.get(TableRecord, {"id": "t2", "table_id": "tasks"})
    record.data = {**record.data, "desc": "其他用户刚刚更新"}
    record.version += 1
    await action_db.commit()

    with pytest.raises(AiActionPlanError, match="changed after preview"):
        await ai_action_plan_service.apply(
            action_db,
            user_id=1,
            request=AiActionPlanApplyRequest.model_validate(
                {"planId": preview["planId"]}
            ),
        )

    current = await action_db.get(TableRecord, {"id": "t2", "table_id": "tasks"})
    assert current.data["priority"] == "low"


@pytest.mark.asyncio
async def test_hidden_row_action_is_blocked_during_preview(action_db):
    action_db.add(
        TableRowPermissionPolicy(
            table_id="tasks",
            mode="creator",
            member_field_id=None,
        )
    )
    await action_db.commit()

    diagnosis = await diagnosis_id(action_db, user_id=2)
    with pytest.raises(PermissionError, match="not found or no access"):
        await ai_action_plan_service.preview(
            action_db,
            user_id=2,
            request=AiActionPlanPreviewRequest.model_validate(
                {
                    "diagnosisId": diagnosis,
                    "proposedActions": [
                        {
                            "type": "set_priority",
                            "tableId": "tasks",
                            "recordId": "t1",
                            "value": "high",
                            "reason": "尝试修改不可见记录",
                        }
                    ],
                }
            ),
        )


@pytest.mark.asyncio
async def test_non_workspace_assignee_is_blocked_during_preview(action_db):
    diagnosis = await diagnosis_id(action_db)
    with pytest.raises(AiActionPlanError, match="not a workspace member"):
        await ai_action_plan_service.preview(
            action_db,
            user_id=1,
            request=AiActionPlanPreviewRequest.model_validate(
                {
                    "diagnosisId": diagnosis,
                    "proposedActions": [
                        {
                            "type": "assign_owner",
                            "tableId": "tasks",
                            "recordId": "t2",
                            "userId": 4,
                            "reason": "非法负责人",
                        }
                    ],
                }
            ),
        )


@pytest.mark.asyncio
async def test_visible_dependency_cycle_is_rejected_in_preview(action_db):
    diagnosis = await diagnosis_id(action_db)
    with pytest.raises(AiActionPlanError, match="dependency cycle"):
        await ai_action_plan_service.preview(
            action_db,
            user_id=1,
            request=AiActionPlanPreviewRequest.model_validate(
                {
                    "diagnosisId": diagnosis,
                    "proposedActions": [
                        {
                            "type": "set_dependency",
                            "tableId": "tasks",
                            "recordId": "t1",
                            "values": ["t2"],
                            "reason": "这会形成 t1 ↔ t2 循环",
                        }
                    ],
                }
            ),
        )


@pytest.mark.asyncio
async def test_stale_diagnosis_cannot_generate_action_plan(action_db):
    diagnosis = await diagnosis_id(action_db)
    record = await action_db.get(TableRecord, {"id": "t1", "table_id": "tasks"})
    record.data = {**record.data, "priority": "high"}
    record.version += 1
    await action_db.commit()

    with pytest.raises(AiActionPlanError, match="stale"):
        await ai_action_plan_service.preview(
            action_db,
            user_id=1,
            request=AiActionPlanPreviewRequest.model_validate(
                {"diagnosisId": diagnosis}
            ),
        )


@pytest.mark.asyncio
async def test_plan_query_is_isolated_by_user(action_db):
    diagnosis = await diagnosis_id(action_db)
    preview = await ai_action_plan_service.preview(
        action_db,
        user_id=1,
        request=AiActionPlanPreviewRequest.model_validate(
            {
                "diagnosisId": diagnosis,
                "proposedActions": [
                    {
                        "type": "set_priority",
                        "tableId": "tasks",
                        "recordId": "t2",
                        "value": "high",
                        "reason": "风险升级",
                    }
                ],
            }
        ),
    )

    assert (
        await ai_action_plan_service.get_plan(
            action_db,
            user_id=2,
            plan_id=preview["planId"],
        )
        is None
    )


def test_graphql_schema_exposes_ai_action_plan_contract():
    schema_text = schema.as_str()
    assert "previewAiActionPlan(" in schema_text
    assert "applyAiActionPlan(" in schema_text
    assert "aiActionPlan(" in schema_text
