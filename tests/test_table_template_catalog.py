from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableField, TableView, WorkspaceItem
from app.models.table_template import TableTemplate
from app.models.workspace_member import Workspace
from app.services.table_templates import (
    SELF_RELATION_TARGET,
    TemplateValidationError,
    instantiate_template_snapshot,
    normalize_template_snapshot,
    sync_system_templates,
)
from app.services.workspace.db_ops import create_table_db


SYSTEM_TEMPLATE_IDS = [
    "asset_inventory",
    "blank",
    "bug_tracking",
    "campaign_planning",
    "content_calendar",
    "customer_success",
    "expense_tracking",
    "inventory_management",
    "milestone_tracker",
    "onboarding_checklist",
    "procurement_tracker",
    "product_requirements",
    "project_management",
    "recruiting_pipeline",
    "sales_crm",
    "sprint_planning",
]

SYSTEM_TEMPLATE_CATEGORIES = {
    "general",
    "project",
    "product",
    "sales",
    "operations",
    "people",
    "finance",
    "asset",
}


@pytest.fixture
async def template_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'template-catalog-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(Workspace(id="ws-template", name="Template Workspace"))
        session.add(
            WorkspaceItem(
                id="root-template",
                workspace_id="ws-template",
                type="folder",
                name="Root",
                parent_id=None,
                order_index=0,
                default_view_id=None,
            )
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_system_template_sync_is_idempotent_and_keeps_versions_stable(template_db):
    assert await sync_system_templates(template_db) == len(SYSTEM_TEMPLATE_IDS)
    assert await sync_system_templates(template_db) == 0

    result = await template_db.execute(
        select(TableTemplate).order_by(TableTemplate.id)
    )
    templates = result.scalars().all()
    assert [template.id for template in templates] == SYSTEM_TEMPLATE_IDS
    assert {template.category for template in templates} == SYSTEM_TEMPLATE_CATEGORIES
    assert all(template.scope == "system" for template in templates)
    assert all(template.version == 1 for template in templates)
    assert all(template.snapshot.get("fields") for template in templates)
    assert all(template.snapshot.get("views") for template in templates)

    project = next(
        template for template in templates if template.id == "project_management"
    )
    gantt = next(
        view for view in project.snapshot["views"] if view["id"] == "v2"
    )
    calendar = next(
        view for view in project.snapshot["views"] if view["id"] == "v4"
    )
    assert gantt["type"] == "gantt"
    assert gantt["config"]["ganttConfig"] == {
        "startFieldId": "f6",
        "endFieldId": "f7",
        "progressFieldId": "f4",
    }
    assert calendar["type"] == "calendar"

    member_field = next(
        field for field in project.snapshot["fields"] if field["id"] == "f3"
    )
    assert member_field["type"] == "member"
    assert "options" not in member_field


@pytest.mark.asyncio
async def test_create_table_from_system_template_uses_catalog_snapshot_and_counts_usage(
    template_db,
):
    created = await create_table_db(
        template_db,
        "Commercial Project",
        "root-template",
        "v1",
        "ws-template",
        "project_management",
        101,
    )
    table_id = created["id"]

    fields = (
        await template_db.execute(
            select(TableField)
            .where(TableField.table_id == table_id)
            .order_by(TableField.order_index)
        )
    ).scalars().all()
    views = (
        await template_db.execute(
            select(TableView).where(TableView.table_id == table_id)
        )
    ).scalars().all()
    template = (
        await template_db.execute(
            select(TableTemplate).where(TableTemplate.id == "project_management")
        )
    ).scalars().one()

    assert created["defaultViewId"] == "v1"
    assert [field.name for field in fields] == [
        "任务名称",
        "状态",
        "负责人",
        "进度",
        "优先级",
        "开始时间",
        "结束时间",
        "附件",
    ]
    assert next(field for field in fields if field.id == "f8").type == "attachment"
    assert {view.type for view in views} == {"grid", "gantt", "board", "calendar"}
    assert template.usage_count == 1


@pytest.mark.asyncio
async def test_new_sales_template_creates_complete_crm_table_and_counts_usage(template_db):
    created = await create_table_db(
        template_db,
        "Sales Pipeline",
        "root-template",
        "v1",
        "ws-template",
        "sales_crm",
        101,
    )
    table_id = created["id"]

    fields = (
        await template_db.execute(
            select(TableField)
            .where(TableField.table_id == table_id)
            .order_by(TableField.order_index)
        )
    ).scalars().all()
    views = (
        await template_db.execute(
            select(TableView).where(TableView.table_id == table_id)
        )
    ).scalars().all()
    template = (
        await template_db.execute(
            select(TableTemplate).where(TableTemplate.id == "sales_crm")
        )
    ).scalars().one()

    assert created["defaultViewId"] == "v1"
    assert [field.name for field in fields] == [
        "客户名称",
        "销售阶段",
        "销售负责人",
        "联系人",
        "联系方式",
        "预计金额",
        "下次跟进",
        "预计成交",
        "备注",
    ]
    assert next(field for field in fields if field.id == "f6").type == "number"
    assert {view.type for view in views} == {"grid", "board", "calendar"}
    assert template.usage_count == 1


@pytest.mark.asyncio
async def test_unknown_template_fails_closed_without_publishing_workspace_shell(template_db):
    with pytest.raises(TemplateValidationError, match="Template not found or no access"):
        await create_table_db(
            template_db,
            "Broken",
            "root-template",
            "v1",
            "ws-template",
            "does-not-exist",
            101,
        )

    result = await template_db.execute(
        select(WorkspaceItem).where(
            WorkspaceItem.workspace_id == "ws-template",
            WorkspaceItem.type == "table",
            WorkspaceItem.name == "Broken",
        )
    )
    assert result.scalars().first() is None


def test_template_snapshot_sanitizes_member_data_and_remaps_self_relations():
    raw = {
        "fields": [
            {
                "id": "title",
                "name": "Title",
                "type": "text",
            },
            {
                "id": "owner",
                "name": "Owner",
                "type": "member",
                "options": [{"id": "user-1", "label": "Alice"}],
            },
            {
                "id": "parent",
                "name": "Parent",
                "type": "relation",
                "property": {
                    "targetTableId": "source-table",
                    "displayFieldId": "title",
                    "multiple": False,
                },
            },
        ],
        "records": [
            {"id": "r1", "title": "Root", "owner": "user-1", "parent": None},
            {"id": "r2", "title": "Child", "owner": "user-1", "parent": "r1"},
        ],
        "views": [
            {
                "id": "v1",
                "name": "Grid",
                "type": "grid",
                "config": {
                    "filters": [],
                    "sorts": [],
                    "groupConfig": {"fieldId": None, "order": "asc"},
                    "hiddenFieldIds": [],
                },
            }
        ],
    }

    normalized = normalize_template_snapshot(
        raw,
        source_table_id="source-table",
        include_records=True,
    )
    owner = next(field for field in normalized["fields"] if field["id"] == "owner")
    relation = next(
        field for field in normalized["fields"] if field["id"] == "parent"
    )
    assert "options" not in owner
    assert relation["property"]["targetTableId"] == SELF_RELATION_TARGET

    instantiated = instantiate_template_snapshot(normalized, "target-table")
    target_relation = next(
        field for field in instantiated["fields"] if field["id"] == "parent"
    )
    assert target_relation["property"]["targetTableId"] == "target-table"

    records = instantiated["records"]
    assert records[0]["id"] != "r1"
    assert records[1]["id"] != "r2"
    assert records[1]["parent"] == records[0]["id"]


def test_template_snapshot_rejects_cross_table_relation_and_broken_view_reference():
    external_relation = {
        "fields": [
            {"id": "title", "name": "Title", "type": "text"},
            {
                "id": "link",
                "name": "Link",
                "type": "relation",
                "property": {
                    "targetTableId": "another-table",
                    "multiple": True,
                },
            },
        ],
        "views": [{"id": "v1", "name": "Grid", "type": "grid", "config": {}}],
    }
    with pytest.raises(
        TemplateValidationError,
        match="cannot contain relations to another table",
    ):
        normalize_template_snapshot(
            external_relation,
            source_table_id="source-table",
        )

    broken_reference = {
        "fields": [{"id": "title", "name": "Title", "type": "text"}],
        "views": [
            {
                "id": "v1",
                "name": "Gantt",
                "type": "gantt",
                "config": {
                    "ganttConfig": {
                        "startFieldId": "missing",
                        "endFieldId": None,
                        "progressFieldId": None,
                    }
                },
            }
        ],
    }
    with pytest.raises(TemplateValidationError, match="references missing field"):
        normalize_template_snapshot(broken_reference)
