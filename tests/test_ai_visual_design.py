from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.models.ai_visual_design import AiVisualDesignPlan
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import TableField, TableRecord, TableView, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.schemas.ai_visual_design import (
    AiVisualDesignApplyRequest,
    AiVisualDesignPreviewRequest,
)
from app.services.ai_visual_design import AiVisualDesignError, ai_visual_design_service
from app.services.dashboard_runtime import compute_widget_data_for_user


@pytest.fixture
async def visual_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ai-visual-design.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(str(statement).lower())

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        today = datetime.now(timezone.utc).date()
        db.add_all(
            [
                User(id=1, email="alice@example.test", password_hash="x", name="Alice"),
                User(id=2, email="bob@example.test", password_hash="x", name="Bob"),
                Workspace(id="w1", name="Workspace"),
                WorkspaceMember(user_id=1, workspace_id="w1", role=WorkspaceRole.owner),
                WorkspaceMember(user_id=2, workspace_id="w1", role=WorkspaceRole.viewer),
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
                    order_index=3,
                ),
                TableField(id="start", table_id="tasks", name="开始日期", type="date", order_index=4),
                TableField(id="due", table_id="tasks", name="截止日期", type="date", order_index=5),
                TableField(id="progress", table_id="tasks", name="进度", type="progress", order_index=6),
                TableField(id="hours", table_id="tasks", name="P50 工时", type="number", order_index=7),
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
                    order_index=8,
                ),
                TableView(
                    id="v1",
                    table_id="tasks",
                    name="Grid",
                    type="grid",
                    config={
                        "filters": [],
                        "sorts": [],
                        "groupConfig": {"fieldId": None, "order": "asc"},
                        "hiddenFieldIds": [],
                        "toolbar": {"items": ["insertRow", "fields", "filter", "group", "sort", "share"]},
                    },
                ),
                TableRecord(
                    id="t1",
                    table_id="tasks",
                    data={
                        "title": "逾期后端任务",
                        "status": "doing",
                        "owner": [{"id": "1", "name": "Alice"}],
                        "priority": "high",
                        "start": (today - timedelta(days=10)).isoformat(),
                        "due": (today - timedelta(days=2)).isoformat(),
                        "progress": 50,
                        "hours": 12,
                        "dep": [],
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRecord(
                    id="t2",
                    table_id="tasks",
                    data={
                        "title": "已完成任务",
                        "status": "done",
                        "owner": [{"id": "1", "name": "Alice"}],
                        "priority": "medium",
                        "start": (today - timedelta(days=20)).isoformat(),
                        "due": (today - timedelta(days=1)).isoformat(),
                        "progress": 100,
                        "hours": 8,
                        "dep": [],
                    },
                    order_index=2,
                    created_by_user_id=1,
                    version=1,
                ),
                WorkspaceItem(
                    id="dash-existing",
                    workspace_id="w1",
                    type="dashboard",
                    name="现有仪表盘",
                    parent_id="root",
                    order_index=2,
                    default_view_id=None,
                ),
                Dashboard(
                    id="dash-existing",
                    workspace_id="w1",
                    description="当前仪表盘",
                    is_public=False,
                ),
                DashboardWidget(
                    id="ew1",
                    dashboard_id="dash-existing",
                    type="metric",
                    title="任务总数",
                    layout={"x": 0, "y": 0, "w": 4, "h": 6},
                    config={
                        "tableId": "tasks",
                        "dimensionFieldId": None,
                        "metric": {"aggregation": "count", "fieldId": None},
                        "filters": [],
                        "sort": {"by": "value", "order": "desc"},
                        "limit": 50,
                    },
                    order_index=0,
                ),
                DashboardWidget(
                    id="ew2",
                    dashboard_id="dash-existing",
                    type="bar",
                    title="状态分布",
                    layout={"x": 0, "y": 6, "w": 6, "h": 12},
                    config={
                        "tableId": "tasks",
                        "dimensionFieldId": "status",
                        "metric": {"aggregation": "count", "fieldId": None},
                        "filters": [],
                        "sort": {"by": "value", "order": "desc"},
                        "limit": 50,
                    },
                    order_index=1,
                ),
                DashboardWidget(
                    id="ew3",
                    dashboard_id="dash-existing",
                    type="bar",
                    title="优先级分布",
                    layout={"x": 6, "y": 6, "w": 6, "h": 12},
                    config={
                        "tableId": "tasks",
                        "dimensionFieldId": "priority",
                        "metric": {"aggregation": "count", "fieldId": None},
                        "filters": [],
                        "sort": {"by": "value", "order": "desc"},
                        "limit": 50,
                    },
                    order_index=2,
                ),
            ]
        )
        await db.commit()
        statements.clear()
        yield db, statements

    event.remove(engine.sync_engine, "before_cursor_execute", capture_sql)
    await engine.dispose()


def view_request(**updates):
    payload = {
        "workspaceId": "w1",
        "prompt": "创建一个只看高优先级、按负责人分组的命名视图",
        "targetType": "view",
        "tableId": "tasks",
    }
    payload.update(updates)
    return AiVisualDesignPreviewRequest.model_validate(payload)


def dashboard_request(**updates):
    payload = {
        "workspaceId": "w1",
        "prompt": "做一个上线前风险仪表盘",
        "targetType": "dashboard",
        "tableId": "tasks",
    }
    payload.update(updates)
    return AiVisualDesignPreviewRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_view_preview_is_schema_only_and_creates_no_business_objects(visual_db):
    db, statements = visual_db
    before_views = len(
        (await db.execute(select(TableView).where(TableView.table_id == "tasks")))
        .scalars()
        .all()
    )
    statements.clear()

    preview = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=view_request(),
    )

    assert preview["target"]["type"] == "view"
    assert preview["performance"]["schemaOnly"] is True
    assert preview["performance"]["recordRowsScanned"] == 0
    assert not any("table_records" in statement for statement in statements)

    proposal = preview["proposal"]["view"]
    assert proposal["type"] == "grid"
    assert proposal["config"]["groupConfig"]["fieldId"] == "owner"
    assert any(
        item["fieldId"] == "priority"
        for item in proposal["config"]["filters"]
    )

    after_views = len(
        (await db.execute(select(TableView).where(TableView.table_id == "tasks")))
        .scalars()
        .all()
    )
    assert after_views == before_views
    assert await db.get(AiVisualDesignPlan, preview["planId"]) is not None


@pytest.mark.asyncio
async def test_view_apply_uses_normal_table_view_model_and_is_idempotent(visual_db):
    db, _ = visual_db
    preview = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=view_request(),
    )
    first = await ai_visual_design_service.apply(
        db,
        user_id=1,
        request=AiVisualDesignApplyRequest.model_validate(
            {"planId": preview["planId"]}
        ),
    )
    second = await ai_visual_design_service.apply(
        db,
        user_id=1,
        request=AiVisualDesignApplyRequest.model_validate(
            {"planId": preview["planId"]}
        ),
    )

    assert first["status"] == "applied"
    assert first["targetType"] == "view"
    assert second["idempotent"] is True
    created = await db.get(
        TableView,
        {"id": first["view"]["id"], "table_id": "tasks"},
    )
    assert created is not None
    assert created.config["filters"] == first["view"]["config"]["filters"]
    assert created.config["groupConfig"]["fieldId"] == "owner"


@pytest.mark.asyncio
async def test_stored_view_preview_preserves_hidden_fields_on_apply(visual_db):
    db, _ = visual_db
    current = {
        "kind": "view",
        "rationale": "只展示最重要的字段。",
        "view": {
            "tableId": "tasks",
            "name": "精简任务视图",
            "type": "grid",
            "filters": [],
            "sorts": [{"fieldId": "due", "order": "asc"}],
            "groupConfig": {"fieldId": "status", "order": "asc"},
            "visibleFieldIds": ["title", "status", "due"],
        },
    }
    preview = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=view_request(
            prompt="创建精简任务视图",
            currentProposal=current,
            instruction="保持这个设计",
        ),
    )
    hidden_before = set(preview["proposal"]["view"]["config"]["hiddenFieldIds"])
    assert "owner" in hidden_before
    assert "hours" in hidden_before

    result = await ai_visual_design_service.apply(
        db,
        user_id=1,
        request=AiVisualDesignApplyRequest.model_validate(
            {"planId": preview["planId"]}
        ),
    )
    created = await db.get(
        TableView,
        {"id": result["view"]["id"], "table_id": "tasks"},
    )
    assert set(created.config["hiddenFieldIds"]) == hidden_before


@pytest.mark.asyncio
async def test_schema_change_after_preview_blocks_view_apply(visual_db):
    db, _ = visual_db
    preview = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=view_request(),
    )
    priority = await db.get(TableField, {"id": "priority", "table_id": "tasks"})
    priority.name = "紧急程度"
    await db.commit()

    with pytest.raises(AiVisualDesignError, match="schema or source permissions changed"):
        await ai_visual_design_service.apply(
            db,
            user_id=1,
            request=AiVisualDesignApplyRequest.model_validate(
                {"planId": preview["planId"]}
            ),
        )


@pytest.mark.asyncio
async def test_dashboard_preview_has_three_meaningful_components_and_no_row_scan(visual_db):
    db, statements = visual_db
    statements.clear()
    preview = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=dashboard_request(),
    )

    assert preview["performance"]["recordRowsScanned"] == 0
    assert preview["performance"]["dashboardWidgetsUseServerAggregation"] is True
    assert preview["performance"]["relationFieldsAvoided"] is True
    assert not any("table_records" in statement for statement in statements)
    widgets = preview["proposal"]["dashboard"]["widgets"]
    assert len(widgets) >= 3
    assert all(widget["config"]["tableId"] == "tasks" for widget in widgets)
    assert all(widget["config"].get("dimensionFieldId") != "dep" for widget in widgets)
    assert any(widget["title"] == "逾期未完成任务" for widget in widgets)


@pytest.mark.asyncio
async def test_dashboard_apply_creates_existing_dashboard_models_and_date_filter_executes(visual_db):
    db, _ = visual_db
    preview = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=dashboard_request(),
    )
    result = await ai_visual_design_service.apply(
        db,
        user_id=1,
        request=AiVisualDesignApplyRequest.model_validate(
            {"planId": preview["planId"]}
        ),
    )

    assert result["status"] == "applied"
    dashboard_id = result["dashboard"]["id"]
    item = await db.get(WorkspaceItem, dashboard_id)
    dashboard = await db.get(Dashboard, dashboard_id)
    widgets = (
        await db.execute(
            select(DashboardWidget)
            .where(DashboardWidget.dashboard_id == dashboard_id)
            .order_by(DashboardWidget.order_index)
        )
    ).scalars().all()
    assert item is not None and item.type == "dashboard"
    assert dashboard is not None
    assert len(widgets) >= 3

    overdue = next(widget for widget in widgets if widget.title == "逾期未完成任务")
    assert any(
        item["operator"] == "before"
        for item in overdue.config["filters"]
    )
    data = await compute_widget_data_for_user(db, overdue, user_id=1)
    assert data["rows"][0]["value"] == 1.0

    owner_load = next(widget for widget in widgets if widget.title == "负责人任务负载")
    owner_data = await compute_widget_data_for_user(db, owner_load, user_id=1)
    assert any(row.get("dimension") == "Alice" for row in owner_data["rows"])


@pytest.mark.asyncio
async def test_invalid_string_sum_is_blocked_before_save(visual_db):
    db, _ = visual_db
    proposal = {
        "kind": "dashboard",
        "rationale": "无效配置",
        "dashboard": {
            "name": "错误仪表盘",
            "widgets": [
                {
                    "key": "bad",
                    "type": "metric",
                    "title": "错误求和",
                    "tableId": "tasks",
                    "metric": {"aggregation": "sum", "fieldId": "title"},
                    "purpose": "不应该允许字符串求和",
                },
                {
                    "key": "ok1",
                    "type": "metric",
                    "title": "任务总数",
                    "tableId": "tasks",
                    "metric": {"aggregation": "count"},
                },
                {
                    "key": "ok2",
                    "type": "bar",
                    "title": "状态分布",
                    "tableId": "tasks",
                    "dimensionFieldId": "status",
                    "metric": {"aggregation": "count"},
                },
            ],
        },
    }
    with pytest.raises(AiVisualDesignError, match="cannot be used with text"):
        await ai_visual_design_service.preview(
            db,
            user_id=1,
            request=dashboard_request(
                currentProposal=proposal,
                instruction="保持当前配置",
            ),
        )


@pytest.mark.asyncio
async def test_relation_dimension_is_blocked_for_performance(visual_db):
    db, _ = visual_db
    proposal = {
        "kind": "dashboard",
        "dashboard": {
            "name": "关系字段仪表盘",
            "widgets": [
                {
                    "key": "bad",
                    "type": "bar",
                    "title": "按依赖分组",
                    "tableId": "tasks",
                    "dimensionFieldId": "dep",
                    "metric": {"aggregation": "count"},
                },
                {
                    "key": "ok1",
                    "type": "metric",
                    "title": "总数",
                    "tableId": "tasks",
                    "metric": {"aggregation": "count"},
                },
                {
                    "key": "ok2",
                    "type": "bar",
                    "title": "状态",
                    "tableId": "tasks",
                    "dimensionFieldId": "status",
                    "metric": {"aggregation": "count"},
                },
            ],
        },
    }
    with pytest.raises(AiVisualDesignError, match="avoid relation dimensions"):
        await ai_visual_design_service.preview(
            db,
            user_id=1,
            request=dashboard_request(
                currentProposal=proposal,
                instruction="保持当前配置",
            ),
        )


@pytest.mark.asyncio
async def test_existing_dashboard_natural_language_refine_and_concurrent_change_guard(visual_db):
    db, _ = visual_db
    preview = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=AiVisualDesignPreviewRequest.model_validate(
            {
                "workspaceId": "w1",
                "prompt": "优化当前仪表盘",
                "targetType": "dashboard",
                "dashboardId": "dash-existing",
                "instruction": "把主要比较图换成折线图",
            }
        ),
    )
    assert any(
        widget["type"] == "line"
        for widget in preview["proposal"]["dashboard"]["widgets"]
    )

    widget = await db.get(DashboardWidget, "ew2")
    widget.title = "其他用户刚改过标题"
    await db.commit()

    with pytest.raises(AiVisualDesignError, match="changed after preview"):
        await ai_visual_design_service.apply(
            db,
            user_id=1,
            request=AiVisualDesignApplyRequest.model_validate(
                {"planId": preview["planId"]}
            ),
        )


@pytest.mark.asyncio
async def test_recent_thirty_days_refinement_generates_editable_date_filter(visual_db):
    db, _ = visual_db
    base = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=dashboard_request(),
    )
    current = ai_visual_design_service._normalized_to_model(base["proposal"])
    refined = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=dashboard_request(
            prompt="调整上线风险仪表盘",
            currentProposal=current.model_dump(mode="json", by_alias=True),
            instruction="把指标改成最近 30 天",
        ),
    )
    widgets = refined["proposal"]["dashboard"]["widgets"]
    assert any(
        any(
            item["fieldId"] in {"due", "start"} and item["operator"] == "after"
            for item in widget["config"].get("filters", [])
        )
        for widget in widgets
    )


@pytest.mark.asyncio
async def test_kanban_generation_matches_existing_select_group_constraint(visual_db):
    db, _ = visual_db
    proposal = {
        "kind": "view",
        "view": {
            "tableId": "tasks",
            "name": "错误负责人看板",
            "type": "board",
            "filters": [],
            "sorts": [],
            "groupConfig": {"fieldId": "owner", "order": "asc"},
            "visibleFieldIds": [],
        },
    }
    with pytest.raises(AiVisualDesignError, match="Kanban grouping must use a select field"):
        await ai_visual_design_service.preview(
            db,
            user_id=1,
            request=view_request(
                prompt="创建负责人看板",
                currentProposal=proposal,
                instruction="保持当前设计",
            ),
        )


@pytest.mark.asyncio
async def test_viewer_cannot_generate_writable_view(visual_db):
    db, _ = visual_db
    with pytest.raises(PermissionError, match="edit permission"):
        await ai_visual_design_service.preview(
            db,
            user_id=2,
            request=view_request(),
        )


@pytest.mark.asyncio
async def test_plan_query_is_user_isolated(visual_db):
    db, _ = visual_db
    preview = await ai_visual_design_service.preview(
        db,
        user_id=1,
        request=view_request(),
    )
    assert (
        await ai_visual_design_service.get_plan(
            db,
            user_id=2,
            plan_id=preview["planId"],
        )
        is None
    )


def test_graphql_schema_exposes_ai_visual_design_contract():
    schema_text = schema.as_str()
    assert "previewAiVisualDesign(" in schema_text
    assert "applyAiVisualDesign(" in schema_text
    assert "aiVisualDesignPlan(" in schema_text
