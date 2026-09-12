from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.task_planning as planning_module
from app.api.graphql import schema
from app.db.base import Base
from app.models.change_history import ChangeSet
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.task_split import TaskSplitPlan as TaskSplitPlanModel
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.schemas.task_planning import (
    TaskPlanningApplyRequest,
    TaskPlanningDecision,
    TaskPlanningPreviewRequest,
)
from app.schemas.task_split import (
    TaskSplitDependency,
    TaskSplitEstimate,
    TaskSplitNode,
    TaskSplitPlan,
    TaskSplitRequest,
    TaskSplitResponse,
)
from app.services.task_planning import TaskPlanningError, task_planning_service
from app.services.task_split import task_split_service


def sample_plan() -> TaskSplitPlan:
    return TaskSplitPlan(
        goal="博客改版",
        summary="拆成需求、设计和上线准备",
        root=TaskSplitNode(
            key="1",
            title="博客改版",
            objective="完成博客改版",
            depth=0,
            deliverables=["完成后的博客改版项目"],
            acceptanceCriteria=["项目达到上线条件"],
            children=[
                TaskSplitNode(
                    key="1.1",
                    title="需求梳理",
                    description="梳理目标与范围",
                    objective="形成范围说明",
                    depth=1,
                    priority="high",
                    milestone="方案确认",
                    suggestedRole="产品",
                    estimate=TaskSplitEstimate(
                        optimisticHours=2,
                        likelyHours=4,
                        pessimisticHours=6,
                        bufferedHours=5,
                    ),
                    deliverables=["需求清单"],
                    acceptanceCriteria=["范围经负责人确认"],
                    children=[
                        TaskSplitNode(
                            key="1.1.1",
                            title="设计首页",
                            description="完成首页信息架构与视觉设计",
                            objective="交付首页设计",
                            depth=2,
                            priority="high",
                            milestone="设计完成",
                            suggestedRole="设计",
                            estimate=TaskSplitEstimate(
                                optimisticHours=4,
                                likelyHours=8,
                                pessimisticHours=12,
                                bufferedHours=10,
                            ),
                            deliverables=["首页设计稿"],
                            acceptanceCriteria=["设计稿通过评审"],
                        )
                    ],
                ),
                TaskSplitNode(
                    key="1.2",
                    title="准备上线",
                    description="完成上线前检查",
                    objective="具备上线条件",
                    depth=1,
                    priority="urgent",
                    milestone="上线",
                    suggestedRole="开发",
                    estimate=TaskSplitEstimate(
                        optimisticHours=2,
                        likelyHours=3,
                        pessimisticHours=5,
                        bufferedHours=4,
                    ),
                    deliverables=["上线检查清单"],
                    acceptanceCriteria=["所有阻塞项已关闭"],
                    dependsOn=["1.1"],
                ),
            ],
        ),
        dependencies=[
            TaskSplitDependency(
                predecessorKey="1.1",
                successorKey="1.2",
                dependencyType="blocks",
                rationale="方案确认后才能上线",
            )
        ],
        mermaid="",
    )


@pytest.fixture
async def planning_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'task-planning.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                User(
                    id=1,
                    email="alice@example.test",
                    password_hash="test-only",
                    name="Alice",
                ),
                Workspace(id="w1", name="Workspace"),
                WorkspaceMember(
                    user_id=1,
                    workspace_id="w1",
                    role=WorkspaceRole.owner,
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
                    name="任务",
                    parent_id="root",
                    order_index=1,
                    default_view_id="v1",
                ),
                TableField(
                    id="f1",
                    table_id="tasks",
                    name="任务名称",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="f2",
                    table_id="tasks",
                    name="任务描述",
                    type="text",
                    order_index=1,
                ),
                TableField(
                    id="f3",
                    table_id="tasks",
                    name="优先级",
                    type="select",
                    options=[
                        {"id": "low", "label": "低"},
                        {"id": "medium", "label": "中"},
                        {"id": "high", "label": "高"},
                        {"id": "urgent", "label": "紧急"},
                    ],
                    order_index=2,
                ),
                TableField(
                    id="f4",
                    table_id="tasks",
                    name="预计工作量",
                    type="number",
                    order_index=3,
                ),
                TableField(
                    id="f5",
                    table_id="tasks",
                    name="前置依赖",
                    type="relation",
                    property={
                        "targetTableId": "tasks",
                        "displayFieldId": "f1",
                        "multiple": True,
                    },
                    order_index=4,
                ),
                TableRecord(
                    id="existing-design",
                    table_id="tasks",
                    data={
                        "f1": "设计首页",
                        "f2": "",
                        "f3": "medium",
                        "f4": None,
                        "f5": [],
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRecord(
                    id="existing-release",
                    table_id="tasks",
                    data={
                        "f1": "准备上线",
                        "f2": "",
                        "f3": None,
                        "f4": None,
                        "f5": [],
                    },
                    order_index=2,
                    created_by_user_id=1,
                    version=1,
                ),
            ]
        )
        await db.commit()
        yield db

    await engine.dispose()


def test_field_mapping_rejects_wrong_relation_target_and_cardinality():
    fields = [
        TableField(
            id="f_title",
            table_id="tasks",
            name="任务名称",
            type="text",
            order_index=0,
        ),
        TableField(
            id="f_wrong_parent_target",
            table_id="tasks",
            name="父任务",
            type="relation",
            property={
                "targetTableId": "another-table",
                "displayFieldId": "f_title",
                "multiple": False,
            },
            order_index=1,
        ),
        TableField(
            id="f_wrong_dependencies_target",
            table_id="tasks",
            name="前置依赖",
            type="relation",
            property={
                "targetTableId": "another-table",
                "displayFieldId": "f_title",
                "multiple": True,
            },
            order_index=2,
        ),
        TableField(
            id="f_wrong_parent_cardinality",
            table_id="tasks",
            name="上级任务",
            type="relation",
            property={
                "targetTableId": "tasks",
                "displayFieldId": "f_title",
                "multiple": True,
            },
            order_index=3,
        ),
        TableField(
            id="f_wrong_dependency_cardinality",
            table_id="tasks",
            name="依赖任务",
            type="relation",
            property={
                "targetTableId": "tasks",
                "displayFieldId": "f_title",
                "multiple": False,
            },
            order_index=4,
        ),
    ]
    mapping, additions = planning_module._build_field_mapping(fields, "tasks")
    assert mapping["parent"] not in {
        "f_wrong_parent_target",
        "f_wrong_parent_cardinality",
    }
    assert mapping["dependencies"] not in {
        "f_wrong_dependencies_target",
        "f_wrong_dependency_cardinality",
    }
    added = {item["semantic"]: item for item in additions}
    assert added["parent"]["property"] == {
        "targetTableId": "tasks",
        "displayFieldId": "f_title",
        "multiple": False,
    }
    assert added["dependencies"]["property"] == {
        "targetTableId": "tasks",
        "displayFieldId": "f_title",
        "multiple": True,
    }


def test_stable_source_fingerprint_ignores_prompt_paraphrase():
    first = TaskPlanningPreviewRequest(
        goal="把这条资料拆成任务",
        workspaceId="w1",
        targetTableId="tasks",
        sourceReference={
            "type": "qnote",
            "sourceId": "source-123",
            "canonicalUrl": "https://example.test/article",
        },
    )
    second = TaskPlanningPreviewRequest(
        goal="请重新规划这份资料的执行事项，表达方式完全不同",
        workspaceId="w1",
        targetTableId="tasks",
        sourceReference={
            "type": "qnote",
            "sourceId": "source-123",
            "canonicalUrl": "https://example.test/article",
        },
    )
    assert planning_module._source_fingerprint(first) == planning_module._source_fingerprint(second)


def test_field_mapping_does_not_reuse_one_existing_field_for_two_semantics():
    fields = [
        TableField(
            id="f_combo",
            table_id="tasks",
            name="任务描述验收标准",
            type="text",
            order_index=0,
        )
    ]
    mapping, additions = planning_module._build_field_mapping(fields, "tasks")
    mapped_ids = list(mapping.values())
    assert len(mapped_ids) == len(set(mapped_ids))
    assert len([item for item in additions if item["semantic"] in {"description", "acceptance"}]) >= 1


def test_task_split_normalization_remaps_ai_dependency_keys_and_rejects_cycles():
    raw = TaskSplitPlan(
        goal="目标",
        summary="summary",
        root=TaskSplitNode(
            key="root",
            title="目标",
            deliverables=["目标"],
            acceptanceCriteria=["完成"],
            children=[
                TaskSplitNode(
                    key="discover",
                    title="需求梳理",
                    deliverables=["需求"],
                    acceptanceCriteria=["确认"],
                ),
                TaskSplitNode(
                    key="build",
                    title="开发",
                    dependsOn=["discover"],
                    deliverables=["代码"],
                    acceptanceCriteria=["通过测试"],
                ),
            ],
        ),
        dependencies=[
            TaskSplitDependency(
                predecessorKey="discover",
                successorKey="build",
                dependencyType="blocks",
            )
        ],
    )
    normalized = task_split_service._normalize_plan(
        raw,
        TaskSplitRequest(
            prompt="目标",
            maxDepth=3,
            maxChildrenPerNode=8,
        ),
    )
    assert normalized.root.children[0].key == "1.1"
    assert normalized.root.children[1].key == "1.2"
    assert normalized.root.children[1].depends_on == ["1.1"]
    assert any(
        dep.predecessor_key == "1.1" and dep.successor_key == "1.2"
        for dep in normalized.dependencies
    )

    cyclic = TaskSplitPlan(
        goal="目标",
        summary="summary",
        root=TaskSplitNode(
            key="root",
            title="目标",
            deliverables=["目标"],
            acceptanceCriteria=["完成"],
            children=[
                TaskSplitNode(
                    key="a",
                    title="A",
                    dependsOn=["b"],
                    deliverables=["A"],
                    acceptanceCriteria=["A done"],
                ),
                TaskSplitNode(
                    key="b",
                    title="B",
                    dependsOn=["a"],
                    deliverables=["B"],
                    acceptanceCriteria=["B done"],
                ),
            ],
        ),
    )
    with pytest.raises(ValueError, match="cycle"):
        task_split_service._normalize_plan(
            cyclic,
            TaskSplitRequest(prompt="目标", maxDepth=3, maxChildrenPerNode=8),
        )


async def fake_split_run(db, user_id: int, request: TaskSplitRequest):
    plan = sample_plan()
    plan_id = "plan-" + uuid.uuid4().hex
    trace_id = "trace-" + uuid.uuid4().hex
    db.add(
        TaskSplitPlanModel(
            id=plan_id,
            workspace_id=request.workspace_id,
            project_id=request.project_id,
            task_id=request.task_id,
            source_prompt=request.prompt,
            normalized_goal=plan.goal,
            summary=plan.summary,
            mermaid=plan.mermaid,
            context_summary={},
            structured_output=plan.model_dump(mode="json", by_alias=True),
            provider="test",
            model="test-model",
            trace_id=trace_id,
            created_by=str(user_id),
            auto_created=True,
            application_status="previewed",
        )
    )
    await db.commit()
    return TaskSplitResponse(
        traceId=trace_id,
        provider="test",
        model="test-model",
        attempts=1,
        persisted=True,
        planId=plan_id,
        result=plan,
    )


@pytest.mark.asyncio
async def test_preview_maps_schema_detects_duplicates_without_writing_tasks(
    planning_db,
    monkeypatch,
):
    monkeypatch.setattr(task_split_service, "run", fake_split_run)
    before = (
        await planning_db.execute(
            select(func.count()).select_from(TableRecord).where(TableRecord.table_id == "tasks")
        )
    ).scalar_one()

    result = await task_planning_service.preview(
        planning_db,
        user_id=1,
        request=TaskPlanningPreviewRequest(
            goal="完成博客改版",
            workspaceId="w1",
            targetTableId="tasks",
            selectedRecordIds=["existing-design"],
            sourceReference={"type": "qnote", "sourceId": "note-1"},
        ),
    )

    after = (
        await planning_db.execute(
            select(func.count()).select_from(TableRecord).where(TableRecord.table_id == "tasks")
        )
    ).scalar_one()
    assert after == before
    assert result["fieldMapping"]["title"] == "f1"
    assert result["fieldMapping"]["dependencies"] == "f5"
    addition_semantics = {item["semantic"] for item in result["schemaAdditions"]}
    assert {"parent", "deliverable", "acceptance", "milestone", "suggestedRole", "sourceTrace"}.issubset(
        addition_semantics
    )
    assert result["duplicates"]["1.1.1"][0]["recordId"] == "existing-design"
    assert result["duplicates"]["1.1.1"][0]["score"] == 1.0

    row = await planning_db.get(TaskSplitPlanModel, result["planId"])
    assert row.target_table_id == "tasks"
    assert row.source_reference["sourceId"] == "note-1"
    assert row.application_status == "previewed"


@pytest.mark.asyncio
async def test_apply_creates_tree_relations_and_is_idempotent(
    planning_db,
    monkeypatch,
):
    monkeypatch.setattr(task_split_service, "run", fake_split_run)
    preview = await task_planning_service.preview(
        planning_db,
        user_id=1,
        request=TaskPlanningPreviewRequest(
            goal="博客改版",
            workspaceId="w1",
            targetTableId="tasks",
        ),
    )

    result = await task_planning_service.apply(
        planning_db,
        user_id=1,
        request=TaskPlanningApplyRequest(
            planId=preview["planId"],
            workspaceId="w1",
            targetTableId="tasks",
            plan=TaskSplitPlan.model_validate(preview["plan"]),
        ),
    )
    assert result["status"] == "applied"
    assert result["idempotent"] is False
    payload = result["result"]
    assert len(payload["created"]) == 3

    parent_field_id = payload["fieldMapping"]["parent"]
    dep_field_id = payload["fieldMapping"]["dependencies"]
    title_field_id = payload["fieldMapping"]["title"]
    record_by_key = payload["recordByNodeKey"]

    epic = await planning_db.get(
        TableRecord,
        {"id": record_by_key["1.1"], "table_id": "tasks"},
    )
    design = await planning_db.get(
        TableRecord,
        {"id": record_by_key["1.1.1"], "table_id": "tasks"},
    )
    release = await planning_db.get(
        TableRecord,
        {"id": record_by_key["1.2"], "table_id": "tasks"},
    )
    assert epic.data[title_field_id] == "需求梳理"
    assert design.data[parent_field_id] == epic.id
    assert release.data[dep_field_id] == [epic.id]

    change_sets = list(
        (
            await planning_db.execute(
                select(ChangeSet).where(
                    ChangeSet.table_id == "tasks",
                    ChangeSet.source == "task_planning",
                )
            )
        ).scalars().all()
    )
    assert len(change_sets) == 1

    before_count = (
        await planning_db.execute(
            select(func.count()).select_from(TableRecord).where(TableRecord.table_id == "tasks")
        )
    ).scalar_one()
    second = await task_planning_service.apply(
        planning_db,
        user_id=1,
        request=TaskPlanningApplyRequest(
            planId=preview["planId"],
            workspaceId="w1",
            targetTableId="tasks",
            plan=TaskSplitPlan.model_validate(preview["plan"]),
        ),
    )
    after_count = (
        await planning_db.execute(
            select(func.count()).select_from(TableRecord).where(TableRecord.table_id == "tasks")
        )
    ).scalar_one()
    assert second["idempotent"] is True
    assert after_count == before_count


@pytest.mark.asyncio
async def test_apply_supports_reuse_merge_and_skip_without_overwriting_existing_values(
    planning_db,
    monkeypatch,
):
    monkeypatch.setattr(task_split_service, "run", fake_split_run)
    preview = await task_planning_service.preview(
        planning_db,
        user_id=1,
        request=TaskPlanningPreviewRequest(
            goal="博客改版 reuse",
            workspaceId="w1",
            targetTableId="tasks",
        ),
    )
    result = await task_planning_service.apply(
        planning_db,
        user_id=1,
        request=TaskPlanningApplyRequest(
            planId=preview["planId"],
            workspaceId="w1",
            targetTableId="tasks",
            plan=TaskSplitPlan.model_validate(preview["plan"]),
            decisions=[
                TaskPlanningDecision(
                    nodeKey="1.1.1",
                    action="reuse",
                    recordId="existing-design",
                ),
                TaskPlanningDecision(
                    nodeKey="1.2",
                    action="merge",
                    recordId="existing-release",
                ),
                TaskPlanningDecision(nodeKey="1.1", action="skip"),
            ],
            allowRepeat=True,
        ),
    )
    payload = result["result"]
    assert payload["recordByNodeKey"]["1.1.1"] == "existing-design"
    assert payload["recordByNodeKey"]["1.2"] == "existing-release"
    assert payload["skipped"] == ["1.1"]

    release = await planning_db.get(
        TableRecord,
        {"id": "existing-release", "table_id": "tasks"},
    )
    assert release.data["f1"] == "准备上线"
    assert release.data["f2"] == "完成上线前检查\n\n具备上线条件"
    assert release.data["f3"] == "urgent"
    assert release.version == 2


@pytest.mark.asyncio
async def test_repeated_source_is_deduplicated_across_different_preview_plans(
    planning_db,
    monkeypatch,
):
    monkeypatch.setattr(task_split_service, "run", fake_split_run)
    request = TaskPlanningPreviewRequest(
        goal="相同来源计划",
        workspaceId="w1",
        targetTableId="tasks",
        sourceReference={"sourceId": "same-source"},
    )
    first_preview = await task_planning_service.preview(
        planning_db,
        user_id=1,
        request=request,
    )
    first_apply = await task_planning_service.apply(
        planning_db,
        user_id=1,
        request=TaskPlanningApplyRequest(
            planId=first_preview["planId"],
            workspaceId="w1",
            targetTableId="tasks",
            plan=TaskSplitPlan.model_validate(first_preview["plan"]),
        ),
    )
    assert first_apply["status"] == "applied"

    second_preview = await task_planning_service.preview(
        planning_db,
        user_id=1,
        request=request,
    )
    assert second_preview["previousApplication"]["planId"] == first_preview["planId"]

    count_before = (
        await planning_db.execute(
            select(func.count()).select_from(TableRecord).where(TableRecord.table_id == "tasks")
        )
    ).scalar_one()
    second_apply = await task_planning_service.apply(
        planning_db,
        user_id=1,
        request=TaskPlanningApplyRequest(
            planId=second_preview["planId"],
            workspaceId="w1",
            targetTableId="tasks",
            plan=TaskSplitPlan.model_validate(second_preview["plan"]),
        ),
    )
    count_after = (
        await planning_db.execute(
            select(func.count()).select_from(TableRecord).where(TableRecord.table_id == "tasks")
        )
    ).scalar_one()

    assert second_apply["status"] == "deduplicated"
    assert second_apply["idempotent"] is True
    assert count_after == count_before


@pytest.mark.asyncio
async def test_apply_rolls_back_schema_and_records_on_mid_transaction_failure(
    planning_db,
    monkeypatch,
):
    monkeypatch.setattr(task_split_service, "run", fake_split_run)
    preview = await task_planning_service.preview(
        planning_db,
        user_id=1,
        request=TaskPlanningPreviewRequest(
            goal="需要回滚的计划",
            workspaceId="w1",
            targetTableId="tasks",
        ),
    )
    fields_before = (
        await planning_db.execute(
            select(func.count()).select_from(TableField).where(TableField.table_id == "tasks")
        )
    ).scalar_one()
    records_before = (
        await planning_db.execute(
            select(func.count()).select_from(TableRecord).where(TableRecord.table_id == "tasks")
        )
    ).scalar_one()

    async def fail_history(*args, **kwargs):
        raise RuntimeError("simulated task planning failure")

    monkeypatch.setattr(planning_module, "append_change_set", fail_history)
    with pytest.raises(RuntimeError, match="simulated task planning failure"):
        await task_planning_service.apply(
            planning_db,
            user_id=1,
            request=TaskPlanningApplyRequest(
                planId=preview["planId"],
                workspaceId="w1",
                targetTableId="tasks",
                plan=TaskSplitPlan.model_validate(preview["plan"]),
                allowRepeat=True,
            ),
        )

    fields_after = (
        await planning_db.execute(
            select(func.count()).select_from(TableField).where(TableField.table_id == "tasks")
        )
    ).scalar_one()
    records_after = (
        await planning_db.execute(
            select(func.count()).select_from(TableRecord).where(TableRecord.table_id == "tasks")
        )
    ).scalar_one()
    assert fields_after == fields_before
    assert records_after == records_before
    plan_row = await planning_db.get(TaskSplitPlanModel, preview["planId"])
    assert plan_row.application_status == "failed"
    assert "simulated task planning failure" in plan_row.apply_error


def test_graphql_schema_exposes_task_planning_product_contract():
    schema_text = schema.as_str()
    assert "previewTaskPlanning(" in schema_text
    assert "applyTaskPlanning(" in schema_text
    assert "taskPlanningPlan(" in schema_text
