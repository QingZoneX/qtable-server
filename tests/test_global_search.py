from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.global_search as search_module
from app.api.graphql import schema
from app.core.config import settings
from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, TableRowPermissionPolicy, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.global_search import global_search


@pytest.fixture
async def search_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'global-search-test.db'}"
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
                Workspace(id="w1", name="Alpha Workspace"),
                Workspace(id="w2", name="Private Workspace"),
                WorkspaceMember(
                    user_id=1,
                    workspace_id="w1",
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=2,
                    workspace_id="w2",
                    role=WorkspaceRole.owner,
                ),
                WorkspaceItem(
                    id="root1",
                    workspace_id="w1",
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="folder-alpha",
                    workspace_id="w1",
                    type="folder",
                    name="Alpha Folder",
                    parent_id="root1",
                    order_index=1,
                ),
                WorkspaceItem(
                    id="table-alpha",
                    workspace_id="w1",
                    type="table",
                    name="Alpha Tasks",
                    parent_id="folder-alpha",
                    order_index=2,
                    default_view_id="v-grid",
                ),
                WorkspaceItem(
                    id="dashboard-alpha",
                    workspace_id="w1",
                    type="dashboard",
                    name="Alpha Dashboard",
                    parent_id="root1",
                    order_index=3,
                ),
                WorkspaceItem(
                    id="root2",
                    workspace_id="w2",
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="table-private",
                    workspace_id="w2",
                    type="table",
                    name="Alpha Confidential",
                    parent_id="root2",
                    order_index=1,
                    default_view_id="v1",
                ),
                TableField(
                    id="title",
                    table_id="table-alpha",
                    name="标题",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="description",
                    table_id="table-alpha",
                    name="描述",
                    type="text",
                    order_index=1,
                ),
                TableField(
                    id="title",
                    table_id="table-private",
                    name="标题",
                    type="text",
                    order_index=0,
                ),
                TableRecord(
                    id="r-visible",
                    table_id="table-alpha",
                    data={
                        "title": "Project Alpha",
                        "description": "Visible project result",
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=1,
                ),
                TableRecord(
                    id="r-hidden",
                    table_id="table-alpha",
                    data={
                        "title": "Project Alpha Secret",
                        "description": "This must never leak",
                    },
                    order_index=2,
                    created_by_user_id=2,
                    version=1,
                ),
                TableRecord(
                    id="r-private-workspace",
                    table_id="table-private",
                    data={"title": "Project Alpha Private Workspace"},
                    order_index=1,
                    created_by_user_id=2,
                    version=1,
                ),
                TableRowPermissionPolicy(
                    table_id="table-alpha",
                    mode="creator",
                    member_field_id=None,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_global_search_combines_structure_and_records_with_deep_links(search_db):
    result = await global_search(
        search_db,
        user_id=1,
        keyword="Alpha",
        limit=20,
    )

    by_key = {
        (item["entityType"], item["entityId"]): item
        for item in result["results"]
    }
    assert ("workspace", "w1") in by_key
    assert ("folder", "folder-alpha") in by_key
    assert ("table", "table-alpha") in by_key
    assert ("dashboard", "dashboard-alpha") in by_key
    assert ("record", "r-visible") in by_key

    record = by_key[("record", "r-visible")]
    assert record["table"]["id"] == "table-alpha"
    assert record["workspace"]["id"] == "w1"
    assert record["deepLink"] == "/workbench/table-alpha/v-grid?recordId=r-visible"
    assert record["sourceService"] == "qtable"
    assert {entry["id"] for entry in record["matchedFields"]} == {"title"}


@pytest.mark.asyncio
async def test_row_and_workspace_permissions_never_leak_hidden_search_hits(search_db):
    result = await global_search(
        search_db,
        user_id=1,
        keyword="Alpha",
        limit=50,
    )
    serialized = json.dumps(result, ensure_ascii=False)

    assert "r-hidden" not in serialized
    assert "Project Alpha Secret" not in serialized
    assert "This must never leak" not in serialized
    assert "table-private" not in serialized
    assert "Project Alpha Private Workspace" not in serialized
    assert "totalCount" not in result


@pytest.mark.asyncio
async def test_entity_and_workspace_filters_are_applied_before_results(search_db):
    tables_only = await global_search(
        search_db,
        user_id=1,
        keyword="Alpha",
        entity_types=["table"],
        limit=20,
    )
    assert tables_only["results"]
    assert {item["entityType"] for item in tables_only["results"]} == {"table"}

    inaccessible_workspace = await global_search(
        search_db,
        user_id=1,
        keyword="Alpha",
        workspace_id="w2",
        limit=20,
    )
    assert inaccessible_workspace["results"] == []
    assert inaccessible_workspace["hasMore"] is False


@pytest.mark.asyncio
async def test_opaque_cursor_pages_without_overlap_and_rejects_query_reuse(search_db):
    # Make enough visible matches to require multiple pages.
    search_db.add_all(
        [
            TableRecord(
                id=f"page-{index:02d}",
                table_id="table-alpha",
                data={"title": f"Paging Alpha {index:02d}", "description": ""},
                order_index=10 + index,
                created_by_user_id=1,
                version=1,
            )
            for index in range(12)
        ]
    )
    await search_db.commit()

    first = await global_search(
        search_db,
        user_id=1,
        keyword="Paging Alpha",
        entity_types=["record"],
        limit=5,
    )
    assert len(first["results"]) == 5
    assert first["hasMore"] is True
    assert first["nextCursor"]

    second = await global_search(
        search_db,
        user_id=1,
        keyword="Paging Alpha",
        entity_types=["record"],
        cursor=first["nextCursor"],
        limit=5,
    )
    first_ids = {item["entityId"] for item in first["results"]}
    second_ids = {item["entityId"] for item in second["results"]}
    assert len(second["results"]) == 5
    assert first_ids.isdisjoint(second_ids)

    with pytest.raises(ValueError, match="does not match"):
        await global_search(
            search_db,
            user_id=1,
            keyword="Different query",
            entity_types=["record"],
            cursor=first["nextCursor"],
            limit=5,
        )


@pytest.mark.asyncio
async def test_keyword_filter_runs_in_database_before_python_row_permission_checks(
    search_db,
    monkeypatch,
):
    # Thousands of unrelated rows model a large table. Only the two Needle rows
    # should reach Python visibility evaluation; unmatched rows stay in SQL.
    search_db.add_all(
        [
            TableRecord(
                id=f"bulk-{index}",
                table_id="table-alpha",
                data={
                    "title": f"Unrelated row {index}",
                    "description": "ordinary content",
                },
                order_index=100 + index,
                created_by_user_id=1,
                version=1,
            )
            for index in range(2500)
        ]
    )
    search_db.add_all(
        [
            TableRecord(
                id="needle-visible",
                table_id="table-alpha",
                data={"title": "Needle visible", "description": ""},
                order_index=3000,
                created_by_user_id=1,
                version=1,
            ),
            TableRecord(
                id="needle-hidden",
                table_id="table-alpha",
                data={"title": "Needle hidden", "description": ""},
                order_index=3001,
                created_by_user_id=2,
                version=1,
            ),
        ]
    )
    await search_db.commit()

    original_visibility = search_module.record_is_visible
    evaluated = 0

    def counted_visibility(**kwargs):
        nonlocal evaluated
        evaluated += 1
        return original_visibility(**kwargs)

    monkeypatch.setattr(search_module, "record_is_visible", counted_visibility)

    result = await global_search(
        search_db,
        user_id=1,
        keyword="Needle",
        entity_types=["record"],
        limit=10,
    )

    assert [item["entityId"] for item in result["results"]] == ["needle-visible"]
    assert evaluated <= 2


@pytest.mark.asyncio
async def test_record_by_id_supports_deep_link_without_bypassing_row_permissions(
    search_db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    user_result = await search_db.execute(select(User).where(User.id == 1))
    user = user_result.scalars().one()

    visible = await schema.execute(
        """
        query RecordById($tableId: String!, $recordId: ID!) {
          recordById(tableId: $tableId, recordId: $recordId)
        }
        """,
        variable_values={
            "tableId": "table-alpha",
            "recordId": "r-visible",
        },
        context_value={"db": search_db, "user": user},
    )
    assert visible.errors is None
    assert visible.data["recordById"]["id"] == "r-visible"
    assert visible.data["recordById"]["title"] == "Project Alpha"

    hidden = await schema.execute(
        """
        query RecordById($tableId: String!, $recordId: ID!) {
          recordById(tableId: $tableId, recordId: $recordId)
        }
        """,
        variable_values={
            "tableId": "table-alpha",
            "recordId": "r-hidden",
        },
        context_value={"db": search_db, "user": user},
    )
    assert hidden.data is None
    assert hidden.errors
    assert "Record not found or no access" in str(hidden.errors[0])


def test_graphql_schema_exposes_global_search_contract():
    schema_text = schema.as_str()
    assert "globalSearch(" in schema_text
    assert "recordById(" in schema_text
    assert "entityTypes:" in schema_text
    assert "cursor:" in schema_text
