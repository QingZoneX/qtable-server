from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql.queries.dashboard import DashboardQueries
from app.core.config import settings
from app.db.base import Base
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.relation_engine import RelationValidationError
from app.services.row_permissions import (
    filter_store_for_user,
    set_row_permission_policy,
)
from app.services.smart_table_store import (
    create_records_with_data,
    get_full_store,
    update_record,
)
from app.services.workspace import get_effective_permission_for_item


@pytest.fixture
async def relation_row_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'relation-row-permissions.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        alice = User(
            id=501,
            email="alice-relation@example.test",
            name="Alice",
            password_hash="!test",
        )
        bob = User(
            id=502,
            email="bob-relation@example.test",
            name="Bob",
            password_hash="!test",
        )
        workspace = Workspace(id="ws-relation-row", name="Relation Row Scope")
        source = WorkspaceItem(
            id="table-relation-source",
            workspace_id=workspace.id,
            type="table",
            name="Source",
            parent_id=None,
            order_index=0,
        )
        target = WorkspaceItem(
            id="table-relation-target",
            workspace_id=workspace.id,
            type="table",
            name="Target",
            parent_id=None,
            order_index=1,
        )
        dashboard_item = WorkspaceItem(
            id="dashboard-relation",
            workspace_id=workspace.id,
            type="dashboard",
            name="Relation Dashboard",
            parent_id=None,
            order_index=2,
        )
        session.add_all(
            [
                alice,
                bob,
                workspace,
                WorkspaceMember(
                    user_id=alice.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=bob.id,
                    workspace_id=workspace.id,
                    role=WorkspaceRole.editor,
                ),
                source,
                target,
                dashboard_item,
                TableField(
                    id="source_title",
                    table_id=source.id,
                    name="标题",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="rel_multi",
                    table_id=source.id,
                    name="关联记录",
                    type="relation",
                    property={
                        "targetTableId": target.id,
                        "displayFieldId": "target_title",
                        "multiple": True,
                    },
                    order_index=1,
                ),
                TableField(
                    id="rel_single",
                    table_id=source.id,
                    name="单关联",
                    type="relation",
                    property={
                        "targetTableId": target.id,
                        "displayFieldId": "target_title",
                        "multiple": False,
                    },
                    order_index=2,
                ),
                TableField(
                    id="target_title",
                    table_id=target.id,
                    name="名称",
                    type="text",
                    order_index=0,
                ),
                TableRecord(
                    id="target-alice",
                    table_id=target.id,
                    data={"target_title": "Alice Target"},
                    order_index=1,
                    created_by_user_id=alice.id,
                ),
                TableRecord(
                    id="target-bob",
                    table_id=target.id,
                    data={"target_title": "Bob Hidden Target"},
                    order_index=2,
                    created_by_user_id=bob.id,
                ),
                TableRecord(
                    id="source-mixed",
                    table_id=source.id,
                    data={
                        "source_title": "Mixed",
                        "rel_multi": ["target-alice", "target-bob"],
                        "rel_single": "target-bob",
                    },
                    order_index=1,
                    created_by_user_id=alice.id,
                ),
                TableRecord(
                    id="source-hidden-only",
                    table_id=source.id,
                    data={
                        "source_title": "Hidden only",
                        "rel_multi": ["target-bob"],
                        "rel_single": "target-bob",
                    },
                    order_index=2,
                    created_by_user_id=alice.id,
                ),
                Dashboard(
                    id=dashboard_item.id,
                    workspace_id=workspace.id,
                    description="",
                    is_public=False,
                ),
                DashboardWidget(
                    id="widget-relation",
                    dashboard_id=dashboard_item.id,
                    type="bar",
                    title="Relation distribution",
                    layout={"x": 0, "y": 0, "w": 6, "h": 4},
                    config={
                        "tableId": source.id,
                        "dimensionFieldId": "rel_multi",
                        "metric": {"aggregation": "count"},
                    },
                    order_index=0,
                ),
            ]
        )
        await session.commit()
        await set_row_permission_policy(session, target.id, mode="creator")
        yield session, alice, bob, source, target, dashboard_item

    await engine.dispose()


@pytest.mark.asyncio
async def test_visible_source_rows_mask_hidden_relation_targets(relation_row_db):
    session, alice, _, source, _, _ = relation_row_db
    source_permission = await get_effective_permission_for_item(
        session,
        alice.id,
        source.id,
    )
    store = await get_full_store(session, source.id)

    visible_store, _ = await filter_store_for_user(
        session,
        source.id,
        store,
        user_id=alice.id,
        table_permission=source_permission,
    )
    by_id = {row["id"]: row for row in visible_store["records"]}

    assert by_id["source-mixed"]["rel_multi"] == ["target-alice"]
    assert by_id["source-mixed"]["rel_single"] is None
    assert by_id["source-hidden-only"]["rel_multi"] == []
    assert by_id["source-hidden-only"]["rel_single"] is None


@pytest.mark.asyncio
async def test_relation_write_rejects_hidden_target_id(relation_row_db):
    session, alice, _, source, _, _ = relation_row_db

    with pytest.raises(RelationValidationError, match="not found or no access"):
        await update_record(
            session,
            source.id,
            "source-mixed",
            "rel_multi",
            ["target-bob"],
            user_id=alice.id,
        )

    updated = await update_record(
        session,
        source.id,
        "source-mixed",
        "rel_multi",
        ["target-alice"],
        user_id=alice.id,
    )
    assert updated is not None
    assert updated["rel_multi"] == ["target-alice"]


@pytest.mark.asyncio
async def test_bulk_create_rejects_hidden_relation_target(relation_row_db):
    session, alice, _, source, _, _ = relation_row_db

    with pytest.raises(RelationValidationError, match="not found or no access"):
        await create_records_with_data(
            session,
            source.id,
            [
                {
                    "source_title": "Attempt hidden relation",
                    "rel_multi": ["target-bob"],
                }
            ],
            created_by_user_id=alice.id,
        )


@pytest.mark.asyncio
async def test_manager_can_link_target_rows_despite_row_policy(relation_row_db):
    session, alice, _, source, _, _ = relation_row_db
    membership = await session.get(
        WorkspaceMember,
        {"user_id": alice.id, "workspace_id": source.workspace_id},
    )
    membership.role = WorkspaceRole.owner
    await session.commit()

    updated = await update_record(
        session,
        source.id,
        "source-mixed",
        "rel_multi",
        ["target-bob"],
        user_id=alice.id,
    )
    assert updated is not None
    assert updated["rel_multi"] == ["target-bob"]


@pytest.mark.asyncio
async def test_relation_dashboard_uses_sanitized_target_ids(
    relation_row_db,
    monkeypatch,
):
    session, alice, _, _, _, _ = relation_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    info = SimpleNamespace(context={"db": session, "user": alice})

    result = await DashboardQueries().dashboardWidgetData(
        info,
        widget_id="widget-relation",
    )

    dimensions = [str(row.get("dimension")) for row in result.get("rows", [])]
    assert dimensions
    assert all("target-bob" not in dimension for dimension in dimensions)
    assert any("target-alice" in dimension for dimension in dimensions)
