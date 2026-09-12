from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.models.change_history import ChangeSet
from app.models.member_assignment import MemberAssignmentBatch
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    WorkspaceItem,
)
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.schemas.member_assignment import (
    MemberAssignmentApplyRequest,
    MemberAssignmentPreviewRequest,
    MemberAssignmentWhatIfRequest,
)
from app.services.change_history import undo_change_set
from app.services.member_assignment import MemberAssignmentError, member_assignment_service


@pytest.fixture
async def assignment_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'member-assignment.db'}"
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
                    order_index=1,
                ),
                TableField(
                    id="owner",
                    table_id="tasks",
                    name="负责人",
                    type="member",
                    property={"multiple": True},
                    order_index=2,
                ),
                TableField(id="due", table_id="tasks", name="截止日期", type="date", order_index=3),
                TableField(id="p50", table_id="tasks", name="P50 工时", type="number", order_index=4),
                TableField(id="tags", table_id="tasks", name="技能标签", type="text", order_index=5),
                TableField(id="role", table_id="tasks", name="建议角色", type="text", order_index=6),
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
                TableRecord(
                    id="existing",
                    table_id="tasks",
                    data={
                        "title": "后端基础设施",
                        "status": "doing",
                        "owner": [{"id": "1", "name": "Alice"}],
                        "due": (today + timedelta(days=5)).isoformat(),
                        "p50": 12,
                        "tags": "backend api",
                        "role": "backend",
                        "dep": [],
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRecord(
                    id="backend",
                    table_id="tasks",
                    data={
                        "title": "后端接口",
                        "status": "todo",
                        "owner": [],
                        "due": (today + timedelta(days=2)).isoformat(),
                        "p50": 8,
                        "tags": "backend api",
                        "role": "backend",
                        "dep": ["existing"],
                    },
                    order_index=2,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRecord(
                    id="frontend",
                    table_id="tasks",
                    data={
                        "title": "前端页面",
                        "status": "todo",
                        "owner": [],
                        "due": (today + timedelta(days=3)).isoformat(),
                        "p50": 10,
                        "tags": "frontend react",
                        "role": "frontend",
                        "dep": [],
                    },
                    order_index=3,
                    created_by_user_id=2,
                    version=1,
                ),
                TableRecord(
                    id="done",
                    table_id="tasks",
                    data={
                        "title": "历史后端任务",
                        "status": "done",
                        "owner": [{"id": "1", "name": "Alice"}],
                        "due": (today - timedelta(days=2)).isoformat(),
                        "p50": 6,
                        "tags": "backend api",
                        "role": "backend",
                        "dep": [],
                    },
                    order_index=4,
                    created_by_user_id=1,
                    version=2,
                ),
            ]
        )
        await db.commit()
        yield db

    await engine.dispose()


def preview_request(record_ids=None, **overrides):
    payload = {
        "workspaceId": "w1",
        "tableId": "tasks",
        "recordIds": record_ids or [],
        "memberProfiles": [
            {
                "userId": 1,
                "skillTags": ["backend", "api", "python"],
                "roleTags": ["backend"],
                "capacityHours": 40,
            },
            {
                "userId": 2,
                "skillTags": ["frontend", "react"],
                "roleTags": ["frontend"],
                "capacityHours": 40,
            },
            {
                "userId": 3,
                "skillTags": ["qa", "testing"],
                "roleTags": ["qa"],
                "capacityHours": 40,
            },
        ],
        "topK": 3,
    }
    payload.update(overrides)
    return MemberAssignmentPreviewRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_preview_recommends_top_members_with_load_reason_and_confidence(assignment_db):
    result = await member_assignment_service.preview(
        assignment_db,
        user_id=1,
        request=preview_request(["existing", "backend", "frontend"]),
    )
    tasks = {item["recordId"]: item for item in result["tasks"]}

    assert tasks["existing"]["fixed"] is True
    assert tasks["existing"]["fixedReason"] == "existing_assignee"
    assert tasks["existing"]["recommendations"][0]["userId"] == 1

    backend_top = tasks["backend"]["recommendations"][0]
    frontend_top = tasks["frontend"]["recommendations"][0]
    assert backend_top["userId"] == 1
    assert frontend_top["userId"] == 2
    assert 0.2 <= tasks["backend"]["confidence"] <= 0.95
    assert backend_top["projectedLoadHours"] > backend_top["currentLoadHours"]
    assert any("技能匹配" in reason for reason in backend_top["reasons"])
    assert result["summary"]["visibleRecordCount"] == 4

    batch = await assignment_db.get(MemberAssignmentBatch, result["batchId"])
    assert batch is not None
    assert batch.status == "previewed"


@pytest.mark.asyncio
async def test_capacity_what_if_recomputes_top_recommendation(assignment_db):
    preview = await member_assignment_service.preview(
        assignment_db,
        user_id=1,
        request=preview_request(["backend"]),
    )
    assert preview["tasks"][0]["recommendations"][0]["userId"] == 1

    scenario = await member_assignment_service.what_if(
        assignment_db,
        user_id=1,
        request=MemberAssignmentWhatIfRequest.model_validate(
            {
                "batchId": preview["batchId"],
                "capacityOverrides": {"1": 10},
            }
        ),
    )
    scenario_task = scenario["scenario"]["tasks"][0]
    assert scenario_task["recommendations"][0]["userId"] != 1
    assert "backend" in scenario["changedTopRecommendationRecordIds"]


@pytest.mark.asyncio
async def test_manual_lock_is_respected_and_overload_is_explicit(assignment_db):
    result = await member_assignment_service.preview(
        assignment_db,
        user_id=1,
        request=preview_request(
            ["backend"],
            lockedAssignments={"backend": 2},
            capacityOverrides={"2": 5},
        ),
    )
    task = result["tasks"][0]
    assert task["fixedReason"] == "manual_lock"
    assert task["recommendations"][0]["userId"] == 2
    assert task["recommendations"][0]["locked"] is True
    assert task["recommendations"][0]["conflicts"]
    assert "超过容量" in task["recommendations"][0]["conflicts"][0]


@pytest.mark.asyncio
async def test_preview_rejects_profile_for_non_project_member(assignment_db):
    with pytest.raises(MemberAssignmentError, match="unavailable project member"):
        await member_assignment_service.preview(
            assignment_db,
            user_id=1,
            request=MemberAssignmentPreviewRequest.model_validate(
                {
                    "workspaceId": "w1",
                    "tableId": "tasks",
                    "recordIds": ["backend"],
                    "memberProfiles": [
                        {
                            "userId": 4,
                            "skillTags": ["backend"],
                            "roleTags": ["backend"],
                            "capacityHours": 40,
                        }
                    ],
                }
            ),
        )


@pytest.mark.asyncio
async def test_row_permissions_keep_hidden_tasks_out_of_load_and_scope(assignment_db):
    assignment_db.add(
        TableRowPermissionPolicy(
            table_id="tasks",
            mode="creator",
            member_field_id=None,
        )
    )
    await assignment_db.commit()

    result = await member_assignment_service.preview(
        assignment_db,
        user_id=2,
        request=preview_request(["frontend"]),
    )
    assert result["summary"]["visibleRecordCount"] == 1
    members = {
        item["userId"]: item
        for item in result["summary"]["projectedMembers"]
    }
    assert members[1]["currentLoadHours"] == 0


@pytest.mark.asyncio
async def test_partial_apply_writes_member_changeset_and_is_undoable(assignment_db):
    preview = await member_assignment_service.preview(
        assignment_db,
        user_id=1,
        request=preview_request(["backend", "frontend"]),
    )
    backend = next(item for item in preview["tasks"] if item["recordId"] == "backend")
    selected_user = backend["recommendations"][0]["userId"]

    applied = await member_assignment_service.apply(
        assignment_db,
        user_id=1,
        request=MemberAssignmentApplyRequest.model_validate(
            {
                "batchId": preview["batchId"],
                "workspaceId": "w1",
                "tableId": "tasks",
                "assignments": [
                    {"recordId": "backend", "userId": selected_user},
                ],
            }
        ),
    )
    assert applied["status"] == "partially_applied"
    assert applied["appliedAssignments"]["backend"] == selected_user
    assert len(applied["changeSetIds"]) == 1

    record = await assignment_db.get(
        TableRecord, {"id": "backend", "table_id": "tasks"}
    )
    assert record.version == 2
    assert record.data["owner"] == [str(selected_user)]

    change_set = await assignment_db.get(ChangeSet, applied["changeSetId"])
    assert change_set is not None
    assert change_set.operation == "member_assignment_apply"
    assert change_set.actor_type == "ai"
    assert change_set.trace_id == preview["batchId"]

    await undo_change_set(
        assignment_db,
        change_set_id=applied["changeSetId"],
        actor_id=1,
        source="test",
    )
    restored = await assignment_db.get(
        TableRecord, {"id": "backend", "table_id": "tasks"}
    )
    assert restored.data["owner"] == []
    assert restored.version == 3


@pytest.mark.asyncio
async def test_explicit_reassignment_requires_confirmation_but_can_replace_existing_owner(assignment_db):
    preview = await member_assignment_service.preview(
        assignment_db,
        user_id=1,
        request=preview_request(
            ["existing"],
            preserveExistingAssignees=False,
            lockedAssignments={"existing": 2},
        ),
    )
    task = preview["tasks"][0]
    assert task["fixedReason"] == "manual_lock"
    assert task["ownerUserIds"] == [1]
    assert task["recommendations"][0]["userId"] == 2

    applied = await member_assignment_service.apply(
        assignment_db,
        user_id=1,
        request=MemberAssignmentApplyRequest.model_validate(
            {
                "batchId": preview["batchId"],
                "workspaceId": "w1",
                "tableId": "tasks",
                "assignments": [{"recordId": "existing", "userId": 2}],
            }
        ),
    )
    assert applied["status"] == "applied"
    record = await assignment_db.get(
        TableRecord, {"id": "existing", "table_id": "tasks"}
    )
    assert record.data["owner"] == ["2"]


@pytest.mark.asyncio
async def test_apply_is_idempotent_for_same_partial_selection(assignment_db):
    preview = await member_assignment_service.preview(
        assignment_db,
        user_id=1,
        request=preview_request(["backend"]),
    )
    user_id = preview["tasks"][0]["recommendations"][0]["userId"]
    request = MemberAssignmentApplyRequest.model_validate(
        {
            "batchId": preview["batchId"],
            "workspaceId": "w1",
            "tableId": "tasks",
            "assignments": [{"recordId": "backend", "userId": user_id}],
        }
    )
    first = await member_assignment_service.apply(
        assignment_db, user_id=1, request=request
    )
    second = await member_assignment_service.apply(
        assignment_db, user_id=1, request=request
    )
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["changeSetIds"] == first["changeSetIds"]


@pytest.mark.asyncio
async def test_apply_refuses_silent_overwrite_after_task_changes(assignment_db):
    preview = await member_assignment_service.preview(
        assignment_db,
        user_id=1,
        request=preview_request(["frontend"]),
    )
    user_id = preview["tasks"][0]["recommendations"][0]["userId"]

    record = await assignment_db.get(
        TableRecord, {"id": "frontend", "table_id": "tasks"}
    )
    record.data = {**record.data, "tags": "frontend react urgent"}
    record.version += 1
    await assignment_db.commit()

    with pytest.raises(MemberAssignmentError, match="changed after preview"):
        await member_assignment_service.apply(
            assignment_db,
            user_id=1,
            request=MemberAssignmentApplyRequest.model_validate(
                {
                    "batchId": preview["batchId"],
                    "workspaceId": "w1",
                    "tableId": "tasks",
                    "assignments": [
                        {"recordId": "frontend", "userId": user_id},
                    ],
                }
            ),
        )
    current = await assignment_db.get(
        TableRecord, {"id": "frontend", "table_id": "tasks"}
    )
    assert current.data["owner"] == []


@pytest.mark.asyncio
async def test_batch_supports_more_than_twenty_tasks(assignment_db):
    today = datetime.now(timezone.utc).date()
    record_ids = []
    for index in range(25):
        rid = f"bulk-{index:02d}"
        record_ids.append(rid)
        assignment_db.add(
            TableRecord(
                id=rid,
                table_id="tasks",
                data={
                    "title": f"批量任务 {index}",
                    "status": "todo",
                    "owner": [],
                    "due": (today + timedelta(days=5 + index)).isoformat(),
                    "p50": 2 + index % 4,
                    "tags": "backend api" if index % 2 == 0 else "frontend react",
                    "role": "backend" if index % 2 == 0 else "frontend",
                    "dep": [],
                },
                order_index=100 + index,
                created_by_user_id=1,
                version=1,
            )
        )
    await assignment_db.commit()

    result = await member_assignment_service.preview(
        assignment_db,
        user_id=1,
        request=preview_request(record_ids),
    )
    assert len(result["tasks"]) == 25
    assert all(item["recommendations"] for item in result["tasks"])
    assert result["summary"]["taskCount"] == 25


def test_graphql_schema_exposes_member_assignment_contract():
    schema_text = schema.as_str()
    assert "previewMemberAssignment(" in schema_text
    assert "memberAssignmentWhatIf(" in schema_text
    assert "applyMemberAssignment(" in schema_text
    assert "memberAssignmentBatch(" in schema_text
