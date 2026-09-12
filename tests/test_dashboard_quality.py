from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    WorkspaceItem,
)
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.dashboard_runtime import (
    DashboardValidationError,
    public_dashboard_payload,
    public_widget_data,
    set_dashboard_publication_actor,
    validate_widget_candidate,
)


@pytest.fixture
async def dashboard_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'dashboard-quality.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        db.add_all(
            [
                User(
                    id=1,
                    email="publisher@example.com",
                    password_hash="x",
                    name="Publisher",
                ),
                User(
                    id=2,
                    email="other@example.com",
                    password_hash="x",
                    name="Other",
                ),
                Workspace(id="ws-a", name="Workspace A"),
                Workspace(id="ws-b", name="Workspace B"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                WorkspaceMember(
                    user_id=1,
                    workspace_id="ws-a",
                    role=WorkspaceRole.viewer,
                ),
                WorkspaceMember(
                    user_id=2,
                    workspace_id="ws-a",
                    role=WorkspaceRole.owner,
                ),
                WorkspaceMember(
                    user_id=2,
                    workspace_id="ws-b",
                    role=WorkspaceRole.owner,
                ),
            ]
        )
        db.add_all(
            [
                WorkspaceItem(
                    id="root-a",
                    workspace_id="ws-a",
                    type="folder",
                    name="Root A",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="root-b",
                    workspace_id="ws-b",
                    type="folder",
                    name="Root B",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="table-a",
                    workspace_id="ws-a",
                    type="table",
                    name="Sales",
                    parent_id="root-a",
                    order_index=1,
                    default_view_id="v1",
                ),
                WorkspaceItem(
                    id="table-b",
                    workspace_id="ws-b",
                    type="table",
                    name="Other Workspace",
                    parent_id="root-b",
                    order_index=1,
                    default_view_id="v1",
                ),
                WorkspaceItem(
                    id="dash-a",
                    workspace_id="ws-a",
                    type="dashboard",
                    name="Executive Dashboard",
                    parent_id="root-a",
                    order_index=2,
                ),
            ]
        )
        db.add(
            Dashboard(
                id="dash-a",
                workspace_id="ws-a",
                description="Commercial dashboard",
                is_public=True,
                public_token="public-token",
                published_by_user_id=1,
            )
        )
        db.add_all(
            [
                TableField(
                    id="status",
                    table_id="table-a",
                    name="Status",
                    type="select",
                    options=[
                        {"id": "open", "label": "Open"},
                        {"id": "done", "label": "Done"},
                    ],
                    order_index=0,
                ),
                TableField(
                    id="amount",
                    table_id="table-a",
                    name="Amount",
                    type="number",
                    order_index=1,
                ),
                TableField(
                    id="title",
                    table_id="table-a",
                    name="Title",
                    type="text",
                    order_index=2,
                ),
                TableField(
                    id="title",
                    table_id="table-b",
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableRecord(
                    id="r1",
                    table_id="table-a",
                    data={"status": "open", "amount": 10, "title": "Mine"},
                    order_index=0,
                    created_by_user_id=1,
                ),
                TableRecord(
                    id="r2",
                    table_id="table-a",
                    data={"status": "done", "amount": 90, "title": "Hidden"},
                    order_index=1,
                    created_by_user_id=2,
                ),
                TableRowPermissionPolicy(
                    table_id="table-a",
                    mode="creator",
                    member_field_id=None,
                ),
                DashboardWidget(
                    id="widget-count",
                    dashboard_id="dash-a",
                    type="metric",
                    title="Visible records",
                    color_scheme={"paletteId": "blue"},
                    layout={"x": 0, "y": 0, "w": 4, "h": 6},
                    config={
                        "tableId": "table-a",
                        "dimensionFieldId": None,
                        "metric": {
                            "aggregation": "count",
                            "fieldId": None,
                        },
                        "filters": [],
                        "sort": {"by": "value", "order": "desc"},
                        "limit": 50,
                    },
                    order_index=1,
                ),
            ]
        )
        await db.commit()
        yield db

    await engine.dispose()


@pytest.mark.asyncio
async def test_public_dashboard_hides_query_internals_and_uses_publisher_row_scope(
    dashboard_db,
):
    payload = await public_dashboard_payload(
        dashboard_db,
        "public-token",
    )
    assert payload["name"] == "Executive Dashboard"
    assert len(payload["widgets"]) == 1

    public_widget = payload["widgets"][0]
    assert public_widget["config"] == {}
    serialized = str(payload)
    assert "table-a" not in serialized
    assert "amount" not in serialized
    assert "creator" not in serialized

    data = await public_widget_data(
        dashboard_db,
        "public-token",
        "widget-count",
    )
    assert data["rows"] == [{"value": 1.0}]


@pytest.mark.asyncio
async def test_public_dashboard_hides_incomplete_draft_widgets(dashboard_db):
    dashboard_db.add(
        DashboardWidget(
            id="widget-draft",
            dashboard_id="dash-a",
            type="bar",
            title="Draft",
            color_scheme=None,
            layout={"x": 4, "y": 0, "w": 4, "h": 6},
            config={
                "tableId": "table-a",
                "dimensionFieldId": None,
                "metric": {"aggregation": "count", "fieldId": None},
                "filters": [],
                "sort": {"by": "value", "order": "desc"},
                "limit": 50,
            },
            order_index=2,
        )
    )
    await dashboard_db.commit()

    payload = await public_dashboard_payload(
        dashboard_db,
        "public-token",
    )
    assert [widget["id"] for widget in payload["widgets"]] == [
        "widget-count"
    ]

    with pytest.raises(
        PermissionError,
        match="widget is unavailable",
    ):
        await public_widget_data(
            dashboard_db,
            "public-token",
            "widget-draft",
        )


@pytest.mark.asyncio
async def test_public_dashboard_stops_when_publisher_loses_source_access(
    dashboard_db,
):
    await dashboard_db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.user_id == 1,
            WorkspaceMember.workspace_id == "ws-a",
        )
    )
    await dashboard_db.commit()

    with pytest.raises(PermissionError, match="source is unavailable"):
        await public_widget_data(
            dashboard_db,
            "public-token",
            "widget-count",
        )


@pytest.mark.asyncio
async def test_widget_validation_rejects_cross_workspace_and_invalid_fields(
    dashboard_db,
):
    with pytest.raises(
        DashboardValidationError,
        match="another workspace",
    ):
        await validate_widget_candidate(
            dashboard_db,
            "dash-a",
            {
                "type": "bar",
                "layout": {"x": 0, "y": 0, "w": 6, "h": 8},
                "config": {
                    "tableId": "table-b",
                    "dimensionFieldId": "title",
                    "metric": {"aggregation": "count"},
                },
            },
        )

    with pytest.raises(
        DashboardValidationError,
        match="missing field",
    ):
        await validate_widget_candidate(
            dashboard_db,
            "dash-a",
            {
                "type": "bar",
                "config": {
                    "tableId": "table-a",
                    "dimensionFieldId": "missing",
                    "metric": {"aggregation": "count"},
                },
            },
        )

    with pytest.raises(
        DashboardValidationError,
        match="numeric field",
    ):
        await validate_widget_candidate(
            dashboard_db,
            "dash-a",
            {
                "type": "metric",
                "config": {
                    "tableId": "table-a",
                    "metric": {
                        "aggregation": "sum",
                        "fieldId": "title",
                    },
                },
            },
        )


@pytest.mark.asyncio
async def test_widget_validation_normalizes_valid_config_and_allows_initial_draft(
    dashboard_db,
):
    draft = await validate_widget_candidate(
        dashboard_db,
        "dash-a",
        {
            "type": "horizontalBar",
            "layout": {"x": 10, "y": 0, "w": 6, "h": 8},
            "config": {},
        },
        require_complete=False,
    )
    assert draft["layout"]["x"] == 6
    assert draft["config"]["tableId"] is None

    valid = await validate_widget_candidate(
        dashboard_db,
        "dash-a",
        {
            "type": "horizontalBar",
            "title": "Amounts",
            "layout": {"x": 0, "y": 0, "w": 6, "h": 8},
            "config": {
                "tableId": "table-a",
                "dimensionFieldId": "status",
                "metric": {
                    "aggregation": "sum",
                    "fieldId": "amount",
                },
                "filters": [
                    {
                        "fieldId": "amount",
                        "operator": "gte",
                        "value": 10,
                    }
                ],
                "sort": {"by": "value", "order": "desc"},
                "limit": 10,
            },
        },
    )
    assert valid["type"] == "horizontalBar"
    assert valid["config"]["metric"] == {
        "aggregation": "sum",
        "fieldId": "amount",
    }
    assert valid["config"]["limit"] == 10


@pytest.mark.asyncio
async def test_publication_actor_is_set_and_cleared(dashboard_db):
    dashboard = (
        await dashboard_db.execute(
            select(Dashboard).where(Dashboard.id == "dash-a")
        )
    ).scalars().one()
    dashboard.published_by_user_id = None
    await dashboard_db.commit()

    published = await set_dashboard_publication_actor(
        dashboard_db,
        "dash-a",
        is_public=True,
        user_id=2,
    )
    assert published.published_by_user_id == 2

    unpublished = await set_dashboard_publication_actor(
        dashboard_db,
        "dash-a",
        is_public=False,
        user_id=2,
    )
    assert unpublished.published_by_user_id is None
