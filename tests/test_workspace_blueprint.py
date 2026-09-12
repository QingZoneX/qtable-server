from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.workspace_blueprint as blueprint_service
from app.api.graphql import schema
from app.core.config import settings
from app.db.base import Base
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import TableField, TableView, WorkspaceItem
from app.models.user import User
from app.models.workspace_generation import WorkspaceGenerationTrace
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.workspace_blueprint import (
    WorkspaceBlueprintError,
    apply_workspace_blueprint,
    normalize_blueprint,
    preview_workspace_blueprint,
)


@pytest.fixture
async def blueprint_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'workspace-blueprint.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
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
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_preview_is_safe_fallback_and_does_not_create_workspace_items(
    blueprint_db,
):
    before = await blueprint_db.execute(select(func.count()).select_from(WorkspaceItem))
    before_count = before.scalar_one()

    result = await preview_workspace_blueprint(
        blueprint_db,
        user_id=1,
        workspace_id="w1",
        parent_id="root",
        goal="3 人在 3 周内完成个人博客改版",
        team_size=3,
        deadline="2026-09-21",
    )

    assert result["generationMode"] == "fallback"
    assert result["warnings"]
    blueprint = result["blueprint"]
    tasks = blueprint["tables"][0]
    field_keys = {field["key"] for field in tasks["fields"]}
    assert {
        "title",
        "assignee",
        "status",
        "priority",
        "startDate",
        "dueDate",
        "workload",
        "dependencies",
    }.issubset(field_keys)
    assert {"grid", "board", "gantt", "calendar"}.issubset(
        {view["type"] for view in tasks["views"]}
    )
    assert blueprint["dashboards"]

    after = await blueprint_db.execute(select(func.count()).select_from(WorkspaceItem))
    assert after.scalar_one() == before_count

    trace = await blueprint_db.get(WorkspaceGenerationTrace, result["traceId"])
    assert trace is not None
    assert trace.status == "previewed"


@pytest.mark.asyncio
async def test_apply_creates_real_project_structure_and_resolves_relation_ids(
    blueprint_db,
):
    preview = await preview_workspace_blueprint(
        blueprint_db,
        user_id=1,
        workspace_id="w1",
        parent_id="root",
        goal="博客改版项目",
        team_size=3,
    )

    applied = await apply_workspace_blueprint(
        blueprint_db,
        trace_id=preview["traceId"],
        user_id=1,
        workspace_id="w1",
        parent_id="root",
        blueprint=preview["blueprint"],
    )
    assert applied["status"] == "applied"
    assert applied["idempotent"] is False

    created = applied["created"]
    assert created["folder"]
    assert len(created["tables"]) == 1
    assert len(created["dashboards"]) == 1

    table_id = created["tables"][0]["id"]
    dashboard_id = created["dashboards"][0]["id"]

    item_result = await blueprint_db.execute(
        select(WorkspaceItem).where(WorkspaceItem.id == table_id)
    )
    table_item = item_result.scalars().one()
    assert table_item.parent_id == created["folder"]["id"]
    assert table_item.default_view_id

    fields_result = await blueprint_db.execute(
        select(TableField)
        .where(TableField.table_id == table_id)
        .order_by(TableField.order_index)
    )
    fields = list(fields_result.scalars().all())
    by_name = {field.name: field for field in fields}
    assignee = by_name["负责人"]
    assert assignee.type == "member"
    assert {option["id"] for option in assignee.options} == {"1", "2"}
    assert {option["label"] for option in assignee.options} == {"Alice", "Bob"}

    assert "前置依赖" in by_name
    relation = by_name["前置依赖"]
    assert relation.type == "relation"
    assert relation.property["targetTableId"] == table_id
    assert relation.property["displayFieldId"] == by_name["任务名称"].id
    assert relation.property["multiple"] is True

    views_result = await blueprint_db.execute(
        select(TableView).where(TableView.table_id == table_id)
    )
    views = list(views_result.scalars().all())
    assert {"grid", "board", "gantt", "calendar"}.issubset(
        {view.type for view in views}
    )
    gantt = next(view for view in views if view.type == "gantt")
    assert gantt.config["ganttConfig"]["startFieldId"] == by_name["开始时间"].id
    assert gantt.config["ganttConfig"]["endFieldId"] == by_name["截止时间"].id

    dashboard = await blueprint_db.get(Dashboard, dashboard_id)
    assert dashboard is not None
    widgets_result = await blueprint_db.execute(
        select(DashboardWidget).where(DashboardWidget.dashboard_id == dashboard_id)
    )
    widgets = list(widgets_result.scalars().all())
    assert len(widgets) == 2
    assert all(widget.config["tableId"] == table_id for widget in widgets)

    trace = await blueprint_db.get(WorkspaceGenerationTrace, preview["traceId"])
    assert trace.status == "applied"
    assert trace.created_items["tables"][0]["id"] == table_id


@pytest.mark.asyncio
async def test_apply_is_idempotent_after_success(blueprint_db):
    preview = await preview_workspace_blueprint(
        blueprint_db,
        user_id=1,
        workspace_id="w1",
        parent_id="root",
        goal="发布新版官网",
    )
    first = await apply_workspace_blueprint(
        blueprint_db,
        trace_id=preview["traceId"],
        user_id=1,
        workspace_id="w1",
        parent_id="root",
        blueprint=preview["blueprint"],
    )
    before = await blueprint_db.execute(select(func.count()).select_from(WorkspaceItem))
    before_count = before.scalar_one()

    second = await apply_workspace_blueprint(
        blueprint_db,
        trace_id=preview["traceId"],
        user_id=1,
        workspace_id="w1",
        parent_id="root",
        blueprint=preview["blueprint"],
    )
    after = await blueprint_db.execute(select(func.count()).select_from(WorkspaceItem))

    assert second["idempotent"] is True
    assert second["created"] == first["created"]
    assert after.scalar_one() == before_count


@pytest.mark.asyncio
async def test_trace_is_user_scoped(blueprint_db):
    preview = await preview_workspace_blueprint(
        blueprint_db,
        user_id=1,
        workspace_id="w1",
        parent_id="root",
        goal="Alice project",
    )
    with pytest.raises(WorkspaceBlueprintError, match="not found or no access"):
        await apply_workspace_blueprint(
            blueprint_db,
            trace_id=preview["traceId"],
            user_id=2,
            workspace_id="w1",
            parent_id="root",
            blueprint=preview["blueprint"],
        )


@pytest.mark.asyncio
async def test_apply_rolls_back_all_workspace_rows_on_mid_transaction_failure(
    blueprint_db,
    monkeypatch,
):
    preview = await preview_workspace_blueprint(
        blueprint_db,
        user_id=1,
        workspace_id="w1",
        parent_id="root",
        goal="Atomic project",
    )
    before = await blueprint_db.execute(select(func.count()).select_from(WorkspaceItem))
    before_count = before.scalar_one()

    async def broken_create(db, blueprint, workspace_id, parent_id):
        db.add(
            WorkspaceItem(
                id="should-not-survive",
                workspace_id=workspace_id,
                type="folder",
                name="Partial",
                parent_id=parent_id,
                order_index=99,
                default_view_id=None,
            )
        )
        await db.flush()
        raise RuntimeError("simulated atomic failure")

    monkeypatch.setattr(blueprint_service, "_create_rows", broken_create)

    with pytest.raises(RuntimeError, match="simulated atomic failure"):
        await apply_workspace_blueprint(
            blueprint_db,
            trace_id=preview["traceId"],
            user_id=1,
            workspace_id="w1",
            parent_id="root",
            blueprint=preview["blueprint"],
        )

    after = await blueprint_db.execute(select(func.count()).select_from(WorkspaceItem))
    assert after.scalar_one() == before_count
    leaked = await blueprint_db.get(WorkspaceItem, "should-not-survive")
    assert leaked is None

    trace = await blueprint_db.get(WorkspaceGenerationTrace, preview["traceId"])
    assert trace.status == "failed"
    assert "simulated atomic failure" in trace.error_message


def test_blueprint_validation_rejects_unknown_relation_target():
    with pytest.raises(WorkspaceBlueprintError, match="Unknown relation target"):
        normalize_blueprint(
            {
                "projectName": "Bad relation",
                "tables": [
                    {
                        "key": "tasks",
                        "name": "Tasks",
                        "fields": [
                            {"key": "title", "name": "Title", "type": "text"},
                            {
                                "key": "dep",
                                "name": "Dependency",
                                "type": "relation",
                                "property": {
                                    "targetTableKey": "missing",
                                    "displayFieldKey": "title",
                                },
                            },
                        ],
                        "views": [{"key": "grid", "name": "Grid", "type": "grid"}],
                    }
                ],
            }
        )


@pytest.mark.asyncio
async def test_graphql_preview_and_apply_enforce_authenticated_workspace_context(
    blueprint_db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    user = await blueprint_db.get(User, 1)

    preview_result = await schema.execute(
        """
        mutation Preview($goal: String!, $workspaceId: String!, $parentId: String!) {
          previewGoalWorkspace(
            goal: $goal
            workspaceId: $workspaceId
            parentId: $parentId
            teamSize: 3
          )
        }
        """,
        variable_values={
            "goal": "3 人博客改版",
            "workspaceId": "w1",
            "parentId": "root",
        },
        context_value={"db": blueprint_db, "user": user},
    )
    assert preview_result.errors is None
    preview = preview_result.data["previewGoalWorkspace"]

    apply_result = await schema.execute(
        """
        mutation Apply(
          $traceId: String!
          $blueprint: JSON!
          $workspaceId: String!
          $parentId: String!
        ) {
          applyGoalWorkspaceBlueprint(
            traceId: $traceId
            blueprint: $blueprint
            workspaceId: $workspaceId
            parentId: $parentId
          )
        }
        """,
        variable_values={
            "traceId": preview["traceId"],
            "blueprint": preview["blueprint"],
            "workspaceId": "w1",
            "parentId": "root",
        },
        context_value={"db": blueprint_db, "user": user},
    )
    assert apply_result.errors is None
    assert apply_result.data["applyGoalWorkspaceBlueprint"]["status"] == "applied"


def test_graphql_schema_exposes_goal_workspace_contract():
    schema_text = schema.as_str()
    assert "previewGoalWorkspace(" in schema_text
    assert "applyGoalWorkspaceBlueprint(" in schema_text
