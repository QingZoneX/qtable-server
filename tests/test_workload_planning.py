from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.models.change_history import ChangeSet
from app.models.estimate_workload import WorkloadEstimateRun
from app.models.smart_table import TableField, TableRecord, TableRowPermissionPolicy, WorkspaceItem
from app.models.user import User
from app.models.workload_planning import WorkloadPlanningBatch
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.schemas.estimate_workload import (
    ContextInsight,
    EstimateConfidence,
    EstimateWorkloadRequest,
    EstimateWorkloadResponse,
    EstimateWorkloadResult,
    HistoricalLearningSummary,
    WorkloadBreakdownItem,
)
from app.schemas.workload_planning import (
    WorkloadPlanningApplyRequest,
    WorkloadPlanningFeedbackRequest,
    WorkloadPlanningPreviewRequest,
    WorkloadPlanningWhatIfRequest,
)
from app.services.estimate_workload import estimate_workload_service
from app.services.workload_planning import (
    WorkloadPlanningError,
    _aggregate_project,
    _build_writeback_mapping,
    workload_planning_service,
)


@pytest.fixture
async def workload_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'workload-planning.db'}"
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
                User(
                    id=2,
                    email="bob@example.test",
                    password_hash="test-only",
                    name="Bob",
                ),
                Workspace(id="w1", name="Workspace One"),
                Workspace(id="w2", name="Workspace Two"),
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
                    id="fdep",
                    table_id="tasks",
                    name="前置依赖",
                    type="relation",
                    property={
                        "targetTableId": "tasks",
                        "displayFieldId": "f1",
                        "multiple": True,
                    },
                    order_index=2,
                ),
                TableField(
                    id="fwork",
                    table_id="tasks",
                    name="预计工作量",
                    type="number",
                    property={"suffix": "h", "precision": 1},
                    order_index=3,
                ),
                TableRecord(
                    id="t1",
                    table_id="tasks",
                    data={
                        "f1": "需求梳理",
                        "f2": "确认范围和验收标准",
                        "fdep": [],
                        "fwork": None,
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=3,
                ),
                TableRecord(
                    id="t2",
                    table_id="tasks",
                    data={
                        "f1": "首页开发",
                        "f2": "实现博客首页",
                        "fdep": ["t1"],
                        "fwork": None,
                    },
                    order_index=2,
                    created_by_user_id=1,
                    version=2,
                ),
                TableRecord(
                    id="t3",
                    table_id="tasks",
                    data={
                        "f1": "部署准备",
                        "f2": "准备部署清单",
                        "fdep": [],
                        "fwork": None,
                    },
                    order_index=3,
                    created_by_user_id=1,
                    version=1,
                ),
            ]
        )
        await db.commit()
        yield db

    await engine.dispose()


def _estimate_result(
    title: str,
    *,
    story_points: float,
    p50: float,
    p90: float,
    confidence: float,
    sample_count: int = 2,
) -> EstimateWorkloadResult:
    return EstimateWorkloadResult(
        summary=f"{title} workload estimate",
        scope=title,
        baseStoryPoints=story_points,
        adjustedStoryPoints=story_points,
        p50Hours=p50,
        p90Hours=p90,
        riskCoefficient=1.0,
        teamCapabilityFactor=1.0,
        techStackFactor=1.0,
        historicalAdjustmentFactor=1.0,
        confidence=EstimateConfidence(
            score=confidence,
            level="high" if confidence >= 0.75 else "medium",
            rationale="Based on task scope and historical calibration.",
        ),
        contextInsight=ContextInsight(
            summary="Task context loaded",
            signals=["clear acceptance criteria"],
        ),
        historicalLearning=HistoricalLearningSummary(
            sampleCount=sample_count,
            feedbackSampleCount=1 if sample_count else 0,
            averageHoursPerStoryPoint=4.0 if sample_count else 0.0,
            averageBias=1.05 if sample_count else 1.0,
            notes=["historical samples available"] if sample_count else [],
        ),
        breakdown=[
            WorkloadBreakdownItem(
                name=title,
                description="single task",
                storyPoints=story_points,
                p50Hours=p50,
                p90Hours=p90,
                riskCoefficient=1.0,
                assumptions=["scope stable"],
            )
        ],
        topRisks=[],
        assumptions=["scope stable"],
        warnings=[],
        formula="P50/P90 calibrated from scope, risk and historical signals",
        nextActions=[],
    )


def _fake_estimates():
    return {
        "t1": (3.0, 10.0, 15.0, 0.85),
        "t2": (5.0, 8.0, 12.0, 0.75),
        "t3": (3.0, 8.0, 16.0, 0.55),
    }


async def fake_estimate_run(db, user_id: int, request: EstimateWorkloadRequest):
    story_points, p50, p90, confidence = _fake_estimates()[str(request.task_id)]
    title = str(request.task_id)
    result = _estimate_result(
        title,
        story_points=story_points,
        p50=p50,
        p90=p90,
        confidence=confidence,
    )
    estimate_id = f"est-{request.task_id}"
    trace_id = f"trace-{request.task_id}"
    existing = await db.get(WorkloadEstimateRun, estimate_id)
    if existing is None:
        db.add(
            WorkloadEstimateRun(
                id=estimate_id,
                workspace_id=request.workspace_id,
                task_id=request.task_id,
                source_prompt=request.prompt,
                normalized_scope=title,
                business_domain=request.business_domain,
                quality_bar=request.quality_bar,
                tech_stack_tags=[],
                work_type_tags=[],
                request_payload=request.model_dump(mode="json", by_alias=True),
                context_summary={},
                historical_summary=result.historical_learning.model_dump(
                    mode="json",
                    by_alias=True,
                ),
                structured_output=result.model_dump(mode="json", by_alias=True),
                confidence_score=confidence,
                base_story_points=story_points,
                adjusted_story_points=story_points,
                p50_hours=p50,
                p90_hours=p90,
                risk_coefficient=1.0,
                team_capability_factor=1.0,
                tech_stack_factor=1.0,
                historical_adjustment_factor=1.0,
                status="estimated",
                provider="test",
                model="test-model",
                trace_id=trace_id,
                created_by=str(user_id),
            )
        )
        await db.commit()
    return EstimateWorkloadResponse(
        traceId=trace_id,
        provider="test",
        model="test-model",
        attempts=1,
        persisted=True,
        estimateId=estimate_id,
        result=result,
    )


def test_writeback_mapping_does_not_reuse_semantically_wrong_numeric_widgets():
    fields = [
        TableField(
            id="f_story_rating",
            table_id="tasks",
            name="Story Point",
            type="rating",
            order_index=0,
        ),
        TableField(
            id="f_conf_progress",
            table_id="tasks",
            name="估算置信度",
            type="progress",
            order_index=1,
        ),
    ]
    mapping, additions = _build_writeback_mapping(fields)
    assert mapping["storyPoints"] != "f_story_rating"
    assert mapping["confidence"] != "f_conf_progress"
    added = {item["semantic"]: item for item in additions}
    assert added["storyPoints"]["type"] == "number"
    assert added["confidence"]["type"] == "number"


def test_dependency_aware_aggregate_separates_total_work_from_calendar_duration():
    tasks = [
        {
            "recordId": "a",
            "title": "A",
            "storyPoints": 3,
            "p50Hours": 10,
            "p90Hours": 15,
            "confidenceScore": 0.8,
            "dependencyRecordIds": [],
        },
        {
            "recordId": "b",
            "title": "B",
            "storyPoints": 5,
            "p50Hours": 8,
            "p90Hours": 12,
            "confidenceScore": 0.7,
            "dependencyRecordIds": ["a"],
        },
        {
            "recordId": "c",
            "title": "C",
            "storyPoints": 3,
            "p50Hours": 8,
            "p90Hours": 16,
            "confidenceScore": 0.5,
            "dependencyRecordIds": [],
        },
    ]
    result = _aggregate_project(
        tasks,
        team_size=2,
        parallel_streams=2,
        deadline=None,
    )
    assert result["totalWorkP50Hours"] == 26
    assert result["criticalPathP50Hours"] == 18
    assert result["criticalPathRecordIds"] == ["a", "b"]
    assert result["calendarP50Hours"] == 18
    assert result["calendarP50Hours"] < result["totalWorkP50Hours"]
    assert result["topUncertainty"][0]["recordId"] == "c"

    cyclic = [dict(item) for item in tasks[:2]]
    cyclic[0]["dependencyRecordIds"] = ["b"]
    with pytest.raises(WorkloadPlanningError, match="cycle"):
        _aggregate_project(
            cyclic,
            team_size=2,
            parallel_streams=2,
            deadline=None,
        )


@pytest.mark.asyncio
async def test_preview_estimates_visible_tasks_without_mutating_qtable(
    workload_db,
    monkeypatch,
):
    estimate_requests: list[EstimateWorkloadRequest] = []

    async def capture_estimate_request(db, user_id: int, request: EstimateWorkloadRequest):
        estimate_requests.append(request)
        return await fake_estimate_run(db, user_id, request)

    monkeypatch.setattr(estimate_workload_service, "run", capture_estimate_request)

    fields_before = (
        await workload_db.execute(
            select(func.count()).select_from(TableField).where(TableField.table_id == "tasks")
        )
    ).scalar_one()
    versions_before = {
        row.id: row.version
        for row in (
            await workload_db.execute(
                select(TableRecord).where(TableRecord.table_id == "tasks")
            )
        ).scalars().all()
    }

    preview = await workload_planning_service.preview(
        workload_db,
        user_id=1,
        request=WorkloadPlanningPreviewRequest(
            workspaceId="w1",
            tableId="tasks",
            recordIds=["t1", "t2", "t3"],
            teamSize=2,
            parallelStreams=2,
        ),
    )

    assert preview["fieldMapping"]["p50Hours"] == "fwork"
    assert {
        "storyPoints",
        "p90Hours",
        "confidence",
        "estimateTrace",
    }.issubset({item["semantic"] for item in preview["schemaAdditions"]})
    task_by_id = {item["recordId"]: item for item in preview["tasks"]}
    assert task_by_id["t1"]["recordVersion"] == 3
    assert task_by_id["t2"]["dependencyRecordIds"] == ["t1"]
    assert preview["aggregate"]["criticalPathRecordIds"] == ["t1", "t2"]
    assert preview["aggregate"]["totalWorkP50Hours"] == 26

    fields_after = (
        await workload_db.execute(
            select(func.count()).select_from(TableField).where(TableField.table_id == "tasks")
        )
    ).scalar_one()
    assert fields_after == fields_before
    versions_after = {
        row.id: row.version
        for row in (
            await workload_db.execute(
                select(TableRecord).where(TableRecord.table_id == "tasks")
            )
        ).scalars().all()
    }
    assert versions_after == versions_before

    batch = await workload_db.get(WorkloadPlanningBatch, preview["batchId"])
    assert batch.status == "previewed"
    estimate_rows = list(
        (
            await workload_db.execute(
                select(WorkloadEstimateRun).where(
                    WorkloadEstimateRun.id.in_(["est-t1", "est-t2", "est-t3"])
                )
            )
        ).scalars().all()
    )
    assert {row.status for row in estimate_rows} == {"previewed"}
    assert [request.table_ids for request in estimate_requests] == [
        ["tasks"],
        ["tasks"],
        ["tasks"],
    ]


@pytest.mark.asyncio
async def test_apply_adds_fields_updates_records_and_writes_one_changeset(
    workload_db,
    monkeypatch,
):
    monkeypatch.setattr(estimate_workload_service, "run", fake_estimate_run)
    preview = await workload_planning_service.preview(
        workload_db,
        user_id=1,
        request=WorkloadPlanningPreviewRequest(
            workspaceId="w1",
            tableId="tasks",
            recordIds=["t1", "t2", "t3"],
            teamSize=2,
            parallelStreams=2,
        ),
    )

    applied = await workload_planning_service.apply(
        workload_db,
        user_id=1,
        request=WorkloadPlanningApplyRequest(
            batchId=preview["batchId"],
            workspaceId="w1",
            tableId="tasks",
        ),
    )
    assert applied["status"] == "applied"
    assert applied["idempotent"] is False
    assert applied["appliedRecordIds"] == ["t1", "t2", "t3"]

    mapping = applied["fieldMapping"]
    t1 = await workload_db.get(TableRecord, {"id": "t1", "table_id": "tasks"})
    t2 = await workload_db.get(TableRecord, {"id": "t2", "table_id": "tasks"})
    assert t1.version == 4
    assert t2.version == 3
    assert t1.data[mapping["p50Hours"]] == 10.0
    assert t1.data[mapping["p90Hours"]] == 15.0
    assert t1.data[mapping["storyPoints"]] == 3.0
    assert t1.data[mapping["confidence"]] == 0.85
    assert preview["batchId"] in t1.data[mapping["estimateTrace"]]

    change_sets = list(
        (
            await workload_db.execute(
                select(ChangeSet).where(
                    ChangeSet.table_id == "tasks",
                    ChangeSet.source == "workload_planning",
                )
            )
        ).scalars().all()
    )
    assert len(change_sets) == 1

    estimate_rows = list(
        (
            await workload_db.execute(
                select(WorkloadEstimateRun).where(
                    WorkloadEstimateRun.id.in_(["est-t1", "est-t2", "est-t3"])
                )
            )
        ).scalars().all()
    )
    assert {row.status for row in estimate_rows} == {"applied"}

    second = await workload_planning_service.apply(
        workload_db,
        user_id=1,
        request=WorkloadPlanningApplyRequest(
            batchId=preview["batchId"],
            workspaceId="w1",
            tableId="tasks",
        ),
    )
    assert second["idempotent"] is True


@pytest.mark.asyncio
async def test_apply_rechecks_row_permission_after_preview(
    workload_db,
    monkeypatch,
):
    monkeypatch.setattr(estimate_workload_service, "run", fake_estimate_run)
    preview = await workload_planning_service.preview(
        workload_db,
        user_id=2,
        request=WorkloadPlanningPreviewRequest(
            workspaceId="w1",
            tableId="tasks",
            recordIds=["t1"],
            teamSize=1,
        ),
    )
    workload_db.add(
        TableRowPermissionPolicy(
            table_id="tasks",
            mode="creator",
            member_field_id=None,
        )
    )
    await workload_db.commit()

    with pytest.raises(PermissionError, match="Record not found or no access"):
        await workload_planning_service.apply(
            workload_db,
            user_id=2,
            request=WorkloadPlanningApplyRequest(
                batchId=preview["batchId"],
                workspaceId="w1",
                tableId="tasks",
            ),
        )

    record = await workload_db.get(TableRecord, {"id": "t1", "table_id": "tasks"})
    assert record.version == 3
    batch = await workload_db.get(WorkloadPlanningBatch, preview["batchId"])
    assert batch.status == "failed"


@pytest.mark.asyncio
async def test_apply_rejects_version_conflict_before_schema_or_other_record_changes(
    workload_db,
    monkeypatch,
):
    monkeypatch.setattr(estimate_workload_service, "run", fake_estimate_run)
    preview = await workload_planning_service.preview(
        workload_db,
        user_id=1,
        request=WorkloadPlanningPreviewRequest(
            workspaceId="w1",
            tableId="tasks",
            recordIds=["t1", "t2"],
            teamSize=2,
        ),
    )

    t2 = await workload_db.get(TableRecord, {"id": "t2", "table_id": "tasks"})
    t2.data = {**t2.data, "f2": "changed after preview"}
    t2.version += 1
    await workload_db.commit()

    fields_before = (
        await workload_db.execute(
            select(func.count()).select_from(TableField).where(TableField.table_id == "tasks")
        )
    ).scalar_one()
    t1_before = await workload_db.get(TableRecord, {"id": "t1", "table_id": "tasks"})
    t1_version_before = t1_before.version

    with pytest.raises(WorkloadPlanningError, match="changed after preview"):
        await workload_planning_service.apply(
            workload_db,
            user_id=1,
            request=WorkloadPlanningApplyRequest(
                batchId=preview["batchId"],
                workspaceId="w1",
                tableId="tasks",
            ),
        )

    fields_after = (
        await workload_db.execute(
            select(func.count()).select_from(TableField).where(TableField.table_id == "tasks")
        )
    ).scalar_one()
    assert fields_after == fields_before
    t1_after = await workload_db.get(TableRecord, {"id": "t1", "table_id": "tasks"})
    assert t1_after.version == t1_version_before
    batch = await workload_db.get(WorkloadPlanningBatch, preview["batchId"])
    assert batch.status == "failed"


@pytest.mark.asyncio
async def test_partial_apply_keeps_success_state_when_later_batch_conflicts(
    workload_db,
    monkeypatch,
):
    monkeypatch.setattr(estimate_workload_service, "run", fake_estimate_run)
    preview = await workload_planning_service.preview(
        workload_db,
        user_id=1,
        request=WorkloadPlanningPreviewRequest(
            workspaceId="w1",
            tableId="tasks",
            recordIds=["t1", "t2"],
            teamSize=2,
        ),
    )
    first = await workload_planning_service.apply(
        workload_db,
        user_id=1,
        request=WorkloadPlanningApplyRequest(
            batchId=preview["batchId"],
            workspaceId="w1",
            tableId="tasks",
            recordIds=["t1"],
        ),
    )
    assert first["status"] == "partially_applied"
    assert first["appliedRecordIds"] == ["t1"]

    t2 = await workload_db.get(TableRecord, {"id": "t2", "table_id": "tasks"})
    t2.data = {**t2.data, "f2": "changed before second partial apply"}
    t2.version += 1
    await workload_db.commit()

    with pytest.raises(WorkloadPlanningError, match="changed after preview"):
        await workload_planning_service.apply(
            workload_db,
            user_id=1,
            request=WorkloadPlanningApplyRequest(
                batchId=preview["batchId"],
                workspaceId="w1",
                tableId="tasks",
                recordIds=["t2"],
            ),
        )

    batch = await workload_db.get(WorkloadPlanningBatch, preview["batchId"])
    assert batch.status == "partially_applied"
    assert batch.result_payload["appliedRecordIds"] == ["t1"]
    assert "changed after preview" in batch.apply_error

    t1 = await workload_db.get(TableRecord, {"id": "t1", "table_id": "tasks"})
    assert t1.version == 4


@pytest.mark.asyncio
async def test_what_if_recomputes_calendar_without_new_ai_call(workload_db):
    batch = WorkloadPlanningBatch(
        id="wpb-what-if",
        user_id=1,
        workspace_id="w1",
        table_id="tasks",
        record_ids=["a", "b", "c", "d"],
        request_payload={
            "workspaceId": "w1",
            "tableId": "tasks",
            "teamSize": 1,
            "parallelStreams": 1,
            "deadline": None,
        },
        result_payload={
            "tasks": [
                {
                    "recordId": task_id,
                    "title": task_id,
                    "storyPoints": 2,
                    "p50Hours": 8,
                    "p90Hours": 12,
                    "confidenceScore": 0.7,
                    "dependencyRecordIds": [],
                }
                for task_id in ["a", "b", "c", "d"]
            ],
            "aggregate": _aggregate_project(
                [
                    {
                        "recordId": task_id,
                        "title": task_id,
                        "storyPoints": 2,
                        "p50Hours": 8,
                        "p90Hours": 12,
                        "confidenceScore": 0.7,
                        "dependencyRecordIds": [],
                    }
                    for task_id in ["a", "b", "c", "d"]
                ],
                team_size=1,
                parallel_streams=1,
                deadline=None,
            ),
            "appliedRecordIds": [],
            "changeSetIds": [],
        },
        record_versions={},
        field_mapping={},
        schema_additions=[],
        status="previewed",
    )
    workload_db.add(batch)
    await workload_db.commit()

    result = await workload_planning_service.what_if(
        workload_db,
        user_id=1,
        request=WorkloadPlanningWhatIfRequest(
            batchId="wpb-what-if",
            teamSize=2,
            parallelStreams=2,
        ),
    )
    assert result["scenario"]["calendarP50Days"] < result["base"]["calendarP50Days"]
    assert result["delta"]["teamSize"] == 1
    assert result["scenario"]["totalWorkP50Hours"] == result["base"]["totalWorkP50Hours"]


@pytest.mark.asyncio
async def test_feedback_only_for_applied_owner_estimates(
    workload_db,
    monkeypatch,
):
    monkeypatch.setattr(estimate_workload_service, "run", fake_estimate_run)

    async def no_cache(_workspace_id):
        return None

    monkeypatch.setattr(estimate_workload_service, "invalidate_history_cache", no_cache)

    preview = await workload_planning_service.preview(
        workload_db,
        user_id=1,
        request=WorkloadPlanningPreviewRequest(
            workspaceId="w1",
            tableId="tasks",
            recordIds=["t1"],
            teamSize=1,
        ),
    )
    with pytest.raises(WorkloadPlanningError, match="only accepted"):
        await workload_planning_service.submit_feedback(
            workload_db,
            user_id=1,
            request=WorkloadPlanningFeedbackRequest(
                batchId=preview["batchId"],
                recordId="t1",
                actualHours=12,
            ),
        )

    await workload_planning_service.apply(
        workload_db,
        user_id=1,
        request=WorkloadPlanningApplyRequest(
            batchId=preview["batchId"],
            workspaceId="w1",
            tableId="tasks",
        ),
    )
    feedback = await workload_planning_service.submit_feedback(
        workload_db,
        user_id=1,
        request=WorkloadPlanningFeedbackRequest(
            batchId=preview["batchId"],
            recordId="t1",
            actualStoryPoints=3,
            actualHours=12,
            outcomeStatus="worse_than_expected",
            accuracyRating=3,
            notes="Scope expanded",
        ),
    )
    assert feedback["status"] == "feedback_applied"
    row = await workload_db.get(WorkloadEstimateRun, "est-t1")
    assert row.actual_hours == 12
    assert row.actual_story_points == 3

    with pytest.raises(WorkloadPlanningError, match="not found or no access"):
        await workload_planning_service.submit_feedback(
            workload_db,
            user_id=2,
            request=WorkloadPlanningFeedbackRequest(
                batchId=preview["batchId"],
                recordId="t1",
                actualHours=12,
            ),
        )


@pytest.mark.asyncio
async def test_historical_learning_is_user_workspace_and_status_scoped(
    workload_db,
    monkeypatch,
):
    base_result = _estimate_result(
        "same prompt",
        story_points=2,
        p50=8,
        p90=12,
        confidence=0.7,
        sample_count=0,
    )
    rows = [
        WorkloadEstimateRun(
            id="hist-good",
            workspace_id="w1",
            source_prompt="same prompt",
            normalized_scope="same prompt",
            request_payload={},
            structured_output=base_result.model_dump(mode="json", by_alias=True),
            confidence_score=0.7,
            base_story_points=2,
            adjusted_story_points=2,
            p50_hours=8,
            p90_hours=12,
            status="estimated",
            created_by="1",
        ),
        WorkloadEstimateRun(
            id="hist-other-workspace",
            workspace_id="w2",
            source_prompt="same prompt",
            normalized_scope="same prompt",
            request_payload={},
            structured_output=base_result.model_dump(mode="json", by_alias=True),
            confidence_score=0.7,
            base_story_points=2,
            adjusted_story_points=2,
            p50_hours=8,
            p90_hours=12,
            status="estimated",
            created_by="1",
        ),
        WorkloadEstimateRun(
            id="hist-other-user",
            workspace_id="w1",
            source_prompt="same prompt",
            normalized_scope="same prompt",
            request_payload={},
            structured_output=base_result.model_dump(mode="json", by_alias=True),
            confidence_score=0.7,
            base_story_points=2,
            adjusted_story_points=2,
            p50_hours=8,
            p90_hours=12,
            status="estimated",
            created_by="2",
        ),
        WorkloadEstimateRun(
            id="hist-preview",
            workspace_id="w1",
            source_prompt="same prompt",
            normalized_scope="same prompt",
            request_payload={},
            structured_output=base_result.model_dump(mode="json", by_alias=True),
            confidence_score=0.7,
            base_story_points=2,
            adjusted_story_points=2,
            p50_hours=8,
            p90_hours=12,
            status="previewed",
            created_by="1",
        ),
    ]
    workload_db.add_all(rows)
    await workload_db.commit()

    async def no_cached(_key):
        return None

    async def ignore_cache(_key, _value, ttl_seconds):
        return None

    monkeypatch.setattr(estimate_workload_service, "_get_cached_json", no_cached)
    monkeypatch.setattr(estimate_workload_service, "_set_cached_json", ignore_cache)

    summary = await estimate_workload_service._load_historical_learning(
        workload_db,
        request=EstimateWorkloadRequest(
            prompt="same prompt",
            workspaceId="w1",
            persistResult=False,
        ),
        agent_context=SimpleNamespace(scope_key="test-scope"),
        user_id=1,
    )
    assert summary.sample_count == 1
    assert [item.estimate_id for item in summary.examples] == ["hist-good"]


def test_graphql_schema_exposes_workload_planning_contract():
    schema_text = schema.as_str()
    assert "previewWorkloadPlanning(" in schema_text
    assert "applyWorkloadPlanning(" in schema_text
    assert "workloadPlanningWhatIf(" in schema_text
    assert "submitWorkloadPlanningFeedback(" in schema_text
    assert "workloadPlanningBatch(" in schema_text
