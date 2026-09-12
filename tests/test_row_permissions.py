from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableField, TableRecord
from app.services.dashboard_analytics import compute_widget_data_db
from app.services.record_query import query_records_db
from app.services.row_permissions import (
    allowed_record_ids,
    ensure_permission_field_change_safe,
    get_row_permission_policy,
    record_is_visible,
    require_record_access,
    set_row_permission_policy,
)
from app.services.smart_table_store import (
    create_record,
    create_records,
    create_records_with_data,
)


@pytest.fixture
async def row_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'row-permission-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                TableField(
                    id="title",
                    table_id="table-row",
                    name="名称",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="owner",
                    table_id="table-row",
                    name="负责人",
                    type="member",
                    options=[
                        {"id": "1", "label": "Alice"},
                        {"id": "2", "label": "Bob"},
                    ],
                    order_index=1,
                ),
                TableField(
                    id="plain",
                    table_id="table-row",
                    name="普通字段",
                    type="text",
                    order_index=2,
                ),
            ]
        )
        session.add_all(
            [
                TableRecord(
                    id="r1",
                    table_id="table-row",
                    data={"title": "Alice created", "owner": ["2"], "plain": "A"},
                    order_index=1,
                    created_by_user_id=1,
                ),
                TableRecord(
                    id="r2",
                    table_id="table-row",
                    data={"title": "Assigned Alice", "owner": ["1"], "plain": "B"},
                    order_index=2,
                    created_by_user_id=2,
                ),
                TableRecord(
                    id="r3",
                    table_id="table-row",
                    data={"title": "Bob only", "owner": ["2"], "plain": "C"},
                    order_index=3,
                    created_by_user_id=2,
                ),
                TableRecord(
                    id="legacy",
                    table_id="table-row",
                    data={"title": "Legacy", "owner": [], "plain": "D"},
                    order_index=4,
                    created_by_user_id=None,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_default_policy_preserves_existing_all_rows_behavior(row_db):
    policy = await get_row_permission_policy(row_db, "table-row")
    assert policy == {"mode": "all", "memberFieldId": None, "enabled": False}

    ids = await allowed_record_ids(
        row_db,
        "table-row",
        user_id=1,
        table_permission="read",
        policy=policy,
    )
    assert ids == {"r1", "r2", "r3", "legacy"}


@pytest.mark.asyncio
async def test_creator_policy_and_manager_bypass(row_db):
    policy = await set_row_permission_policy(
        row_db,
        "table-row",
        mode="creator",
    )
    assert policy["enabled"] is True

    alice_ids = await allowed_record_ids(
        row_db,
        "table-row",
        user_id=1,
        table_permission="update",
        policy=policy,
    )
    assert alice_ids == {"r1"}

    manager_ids = await allowed_record_ids(
        row_db,
        "table-row",
        user_id=1,
        table_permission="manage",
        policy=policy,
    )
    assert manager_ids == {"r1", "r2", "r3", "legacy"}


@pytest.mark.asyncio
async def test_member_field_policy_includes_assignment_and_creator(row_db):
    policy = await set_row_permission_policy(
        row_db,
        "table-row",
        mode="member_field",
        member_field_id="owner",
    )

    alice_ids = await allowed_record_ids(
        row_db,
        "table-row",
        user_id=1,
        table_permission="update",
        policy=policy,
    )
    # r1 is visible because Alice created it even though owner contains Bob.
    # r2 is visible because owner contains Alice.
    assert alice_ids == {"r1", "r2"}

    bob_ids = await allowed_record_ids(
        row_db,
        "table-row",
        user_id=2,
        table_permission="read",
        policy=policy,
    )
    assert bob_ids == {"r1", "r2", "r3"}


@pytest.mark.asyncio
async def test_member_field_policy_validates_schema(row_db):
    with pytest.raises(ValueError, match="member field"):
        await set_row_permission_policy(
            row_db,
            "table-row",
            mode="member_field",
            member_field_id="plain",
        )

    with pytest.raises(ValueError, match="does not exist"):
        await set_row_permission_policy(
            row_db,
            "table-row",
            mode="member_field",
            member_field_id="missing",
        )


@pytest.mark.asyncio
async def test_hidden_and_missing_records_share_same_access_error(row_db):
    policy = await set_row_permission_policy(row_db, "table-row", mode="creator")
    assert policy["mode"] == "creator"

    visible = await require_record_access(
        row_db,
        "table-row",
        "r1",
        user_id=1,
        table_permission="update",
    )
    assert visible.id == "r1"

    for record_id in ("r2", "does-not-exist"):
        with pytest.raises(PermissionError, match="Record not found or no access"):
            await require_record_access(
                row_db,
                "table-row",
                record_id,
                user_id=1,
                table_permission="update",
            )


@pytest.mark.asyncio
async def test_active_permission_member_field_cannot_be_deleted_or_retyped(row_db):
    await set_row_permission_policy(
        row_db,
        "table-row",
        mode="member_field",
        member_field_id="owner",
    )

    with pytest.raises(ValueError, match="used by row permissions"):
        await ensure_permission_field_change_safe(
            row_db,
            "table-row",
            "owner",
            deleting=True,
        )

    with pytest.raises(ValueError, match="must remain a member field"):
        await ensure_permission_field_change_safe(
            row_db,
            "table-row",
            "owner",
            next_type="text",
        )

    # Rename/property changes are safe because the field remains member type.
    await ensure_permission_field_change_safe(
        row_db,
        "table-row",
        "owner",
        next_type=None,
    )


@pytest.mark.asyncio
async def test_record_creation_paths_persist_creator_metadata(row_db):
    one = await create_record(
        row_db,
        "table-row",
        created_by_user_id=7,
    )
    many = await create_records(
        row_db,
        "table-row",
        2,
        created_by_user_id=8,
    )
    with_data = await create_records_with_data(
        row_db,
        "table-row",
        [{"title": "Imported"}],
        created_by_user_id=9,
    )

    ids = [one["id"], *(row["id"] for row in many), with_data[0]["id"]]
    result = await row_db.execute(
        select(TableRecord).where(
            TableRecord.table_id == "table-row",
            TableRecord.id.in_(ids),
        )
    )
    by_id = {record.id: record.created_by_user_id for record in result.scalars().all()}
    assert by_id[one["id"]] == 7
    assert all(by_id[row["id"]] == 8 for row in many)
    assert by_id[with_data[0]["id"]] == 9


@pytest.mark.asyncio
async def test_server_query_row_scope_precedes_filter_sort_and_total_count(row_db):
    fields = [
        {"id": "title", "name": "名称", "type": "text"},
        {
            "id": "owner",
            "name": "负责人",
            "type": "member",
            "options": [
                {"id": "1", "label": "Alice"},
                {"id": "2", "label": "Bob"},
            ],
        },
        {"id": "plain", "name": "普通字段", "type": "text"},
    ]
    policy = await set_row_permission_policy(row_db, "table-row", mode="creator")
    alice_ids = await allowed_record_ids(
        row_db,
        "table-row",
        user_id=1,
        table_permission="read",
        policy=policy,
    )
    result = await query_records_db(
        row_db,
        "table-row",
        fields,
        filters=[],
        sorts=[{"fieldId": "title", "order": "asc"}],
        offset=0,
        limit=100,
        allowed_record_ids=list(alice_ids),
    )

    assert result["totalCount"] == 1
    assert [record["id"] for record in result["records"]] == ["r1"]


@pytest.mark.asyncio
async def test_dashboard_aggregation_uses_only_allowed_rows(row_db):
    result = await compute_widget_data_db(
        row_db,
        {
            "tableId": "table-row",
            "metric": {"aggregation": "count"},
        },
        allowed_record_ids=["r1", "r2"],
    )
    assert result["rows"] == [{"value": 2.0}]

    empty = await compute_widget_data_db(
        row_db,
        {
            "tableId": "table-row",
            "metric": {"aggregation": "count"},
        },
        allowed_record_ids=[],
    )
    assert empty["rows"] == [{"value": 0.0}]


def test_record_visibility_fails_closed_for_stale_policy():
    assert (
        record_is_visible(
            policy={"mode": "member_field", "memberFieldId": "missing"},
            user_id=1,
            table_permission="read",
            created_by_user_id=2,
            data={"owner": ["1"]},
        )
        is False
    )
    assert (
        record_is_visible(
            policy={"mode": "unexpected"},
            user_id=1,
            table_permission="read",
            created_by_user_id=2,
            data={},
        )
        is False
    )
