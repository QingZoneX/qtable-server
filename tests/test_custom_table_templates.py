from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, TableView, WorkspaceItem
from app.models.table_template import TableTemplate
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.smart_table_store import get_full_store
from app.services.table_templates import (
    TemplateValidationError,
    create_custom_template,
    delete_custom_template,
    get_visible_template,
    list_visible_templates,
    set_custom_template_archived,
    sync_system_templates,
    update_custom_template,
)
from app.services.workspace.db_ops import create_table_db


@pytest.fixture
async def custom_template_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'custom-template-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1, email="alice@example.com", password_hash="x", name="Alice"),
                User(id=2, email="bob@example.com", password_hash="x", name="Bob"),
                User(id=3, email="viewer@example.com", password_hash="x", name="Viewer"),
                User(id=4, email="outsider@example.com", password_hash="x", name="Outsider"),
                Workspace(id="ws-a", name="Workspace A"),
                Workspace(id="ws-b", name="Workspace B"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(user_id=1, workspace_id="ws-a", role=WorkspaceRole.owner),
                WorkspaceMember(user_id=2, workspace_id="ws-a", role=WorkspaceRole.editor),
                WorkspaceMember(user_id=3, workspace_id="ws-a", role=WorkspaceRole.viewer),
                WorkspaceMember(user_id=2, workspace_id="ws-b", role=WorkspaceRole.owner),
            ]
        )
        session.add_all(
            [
                WorkspaceItem(
                    id="root-a",
                    workspace_id="ws-a",
                    type="folder",
                    name="Root A",
                    parent_id=None,
                    order_index=0,
                    default_view_id=None,
                ),
                WorkspaceItem(
                    id="root-b",
                    workspace_id="ws-b",
                    type="folder",
                    name="Root B",
                    parent_id=None,
                    order_index=0,
                    default_view_id=None,
                ),
                WorkspaceItem(
                    id="source-a",
                    workspace_id="ws-a",
                    type="table",
                    name="Source A",
                    parent_id="root-a",
                    order_index=1,
                    default_view_id="v1",
                ),
                WorkspaceItem(
                    id="source-b",
                    workspace_id="ws-b",
                    type="table",
                    name="Source B",
                    parent_id="root-b",
                    order_index=1,
                    default_view_id="v1",
                ),
            ]
        )
        session.add_all(
            [
                TableField(
                    id="title",
                    table_id="source-a",
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="owner",
                    table_id="source-a",
                    name="Owner",
                    type="member",
                    options=[{"id": "1", "label": "Alice"}],
                    order_index=1,
                ),
                TableView(
                    id="v1",
                    table_id="source-a",
                    name="Grid",
                    type="grid",
                    config={
                        "filters": [],
                        "sorts": [],
                        "groupConfig": {"fieldId": None, "order": "asc"},
                        "hiddenFieldIds": [],
                    },
                ),
                TableField(
                    id="title",
                    table_id="source-b",
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableView(
                    id="v1",
                    table_id="source-b",
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableRecord(
                    id="source-r1",
                    table_id="source-a",
                    data={"title": "First", "owner": "1"},
                    order_index=0,
                    created_by_user_id=1,
                ),
                TableRecord(
                    id="source-r2",
                    table_id="source-a",
                    data={"title": "Second", "owner": "2"},
                    order_index=1,
                    created_by_user_id=2,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_personal_template_is_private_and_can_recreate_example_records(
    custom_template_db,
):
    template = await create_custom_template(
        custom_template_db,
        source_table_id="source-a",
        name="My private template",
        scope="personal",
        user_id=1,
        include_records=True,
        tags=["project", "project"],
    )

    assert template.scope == "personal"
    assert template.owner_user_id == 1
    assert template.workspace_id is None
    assert len(template.snapshot["records"]) == 2
    owner_field = next(
        field for field in template.snapshot["fields"] if field["id"] == "owner"
    )
    assert "options" not in owner_field
    assert template.tags == ["project"]

    alice_visible = await list_visible_templates(
        custom_template_db,
        user_id=1,
        scope="personal",
    )
    bob_visible = await list_visible_templates(
        custom_template_db,
        user_id=2,
        scope="personal",
    )
    assert [entry["id"] for entry in alice_visible] == [template.id]
    assert alice_visible[0]["canManage"] is True
    assert bob_visible == []

    with pytest.raises(TemplateValidationError, match="not found or no access"):
        await get_visible_template(
            custom_template_db,
            template.id,
            user_id=2,
        )

    created = await create_table_db(
        custom_template_db,
        "From Personal Template",
        "root-a",
        "v1",
        "ws-a",
        template.id,
        1,
    )
    store = await get_full_store(custom_template_db, created["id"])
    assert len(store["records"]) == 2
    assert {record["title"] for record in store["records"]} == {"First", "Second"}
    assert {record["id"] for record in store["records"]}.isdisjoint(
        {"source-r1", "source-r2"}
    )

    refreshed = (
        await custom_template_db.execute(
            select(TableTemplate).where(TableTemplate.id == template.id)
        )
    ).scalars().one()
    assert refreshed.usage_count == 1


@pytest.mark.asyncio
async def test_workspace_template_visibility_and_management_follow_workspace_roles(
    custom_template_db,
):
    template = await create_custom_template(
        custom_template_db,
        source_table_id="source-a",
        name="Workspace template",
        scope="workspace",
        workspace_id="ws-a",
        user_id=2,
        include_records=False,
    )

    owner_visible = await list_visible_templates(
        custom_template_db,
        user_id=1,
        workspace_id="ws-a",
        scope="workspace",
    )
    viewer_visible = await list_visible_templates(
        custom_template_db,
        user_id=3,
        workspace_id="ws-a",
        scope="workspace",
    )
    outsider_visible = await list_visible_templates(
        custom_template_db,
        user_id=4,
        workspace_id="ws-a",
        scope="workspace",
    )
    assert [entry["id"] for entry in owner_visible] == [template.id]
    assert owner_visible[0]["canManage"] is True
    assert [entry["id"] for entry in viewer_visible] == [template.id]
    assert viewer_visible[0]["canManage"] is False
    assert outsider_visible == []

    with pytest.raises(
        TemplateValidationError,
        match="no permission to manage",
    ):
        await update_custom_template(
            custom_template_db,
            template.id,
            user_id=3,
            name="Viewer must not edit",
        )

    updated = await update_custom_template(
        custom_template_db,
        template.id,
        user_id=2,
        name="Editor updated",
        description="Shared reusable workflow",
    )
    assert updated.name == "Editor updated"
    assert updated.version == 2

    archived = await set_custom_template_archived(
        custom_template_db,
        template.id,
        user_id=1,
        archived=True,
    )
    assert archived.status == "archived"
    hidden = await list_visible_templates(
        custom_template_db,
        user_id=2,
        workspace_id="ws-a",
        scope="workspace",
    )
    assert hidden == []

    archived_for_owner = await list_visible_templates(
        custom_template_db,
        user_id=1,
        workspace_id="ws-a",
        scope="workspace",
        include_archived=True,
    )
    archived_for_viewer = await list_visible_templates(
        custom_template_db,
        user_id=3,
        workspace_id="ws-a",
        scope="workspace",
        include_archived=True,
    )
    assert [entry["id"] for entry in archived_for_owner] == [template.id]
    assert archived_for_owner[0]["status"] == "archived"
    assert archived_for_owner[0]["canManage"] is True
    assert archived_for_viewer == []

    active_again = await set_custom_template_archived(
        custom_template_db,
        template.id,
        user_id=1,
        archived=False,
    )
    assert active_again.status == "active"

    assert await delete_custom_template(
        custom_template_db,
        template.id,
        user_id=1,
    )
    deleted = await custom_template_db.execute(
        select(TableTemplate).where(TableTemplate.id == template.id)
    )
    assert deleted.scalars().first() is None


@pytest.mark.asyncio
async def test_workspace_template_cannot_cross_workspace_or_be_created_by_viewer(
    custom_template_db,
):
    with pytest.raises(TemplateValidationError, match="table in that workspace"):
        await create_custom_template(
            custom_template_db,
            source_table_id="source-a",
            name="Cross workspace",
            scope="workspace",
            workspace_id="ws-b",
            user_id=2,
        )

    with pytest.raises(TemplateValidationError, match="No permission"):
        await create_custom_template(
            custom_template_db,
            source_table_id="source-a",
            name="Viewer template",
            scope="workspace",
            workspace_id="ws-a",
            user_id=3,
        )


@pytest.mark.asyncio
async def test_custom_template_refreshes_content_and_versions_only_on_change(
    custom_template_db,
):
    template = await create_custom_template(
        custom_template_db,
        source_table_id="source-a",
        name="Versioned",
        scope="personal",
        user_id=1,
        include_records=False,
    )
    original_hash = template.content_hash

    # No effective metadata/content change keeps the version stable.
    same = await update_custom_template(
        custom_template_db,
        template.id,
        user_id=1,
        name="Versioned",
    )
    assert same.version == 1
    assert same.content_hash == original_hash

    custom_template_db.add(
        TableField(
            id="priority",
            table_id="source-a",
            name="Priority",
            type="number",
            order_index=2,
        )
    )
    await custom_template_db.commit()

    refreshed = await update_custom_template(
        custom_template_db,
        template.id,
        user_id=1,
        source_table_id="source-a",
        include_records=True,
    )
    assert refreshed.version == 2
    assert refreshed.include_records is True
    assert {field["id"] for field in refreshed.snapshot["fields"]} == {
        "title",
        "owner",
        "priority",
    }
    assert len(refreshed.snapshot["records"]) == 2


@pytest.mark.asyncio
async def test_system_templates_are_immutable_through_custom_management_api(
    custom_template_db,
):
    await sync_system_templates(custom_template_db)

    with pytest.raises(
        TemplateValidationError,
        match="no permission to manage",
    ):
        await update_custom_template(
            custom_template_db,
            "blank",
            user_id=1,
            name="Hijacked blank",
        )

    with pytest.raises(
        TemplateValidationError,
        match="no permission to manage",
    ):
        await delete_custom_template(
            custom_template_db,
            "project_management",
            user_id=1,
        )
