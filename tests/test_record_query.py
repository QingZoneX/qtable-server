from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.smart_table import TableRecord
from app.services.record_query import (
    compile_filter_expression,
    query_records_db,
    query_records_in_memory,
)


FIELDS = [
    {"id": "title", "name": "名称", "type": "text"},
    {"id": "score", "name": "分数", "type": "number"},
    {
        "id": "status",
        "name": "状态",
        "type": "select",
        "options": [
            {"id": "todo", "label": "待处理"},
            {"id": "done", "label": "已完成"},
        ],
    },
    {
        "id": "seq",
        "name": "编号",
        "type": "autoNumber",
        "property": {"prefix": "Q-", "digits": 3},
    },
]


RECORDS = [
    {"id": "r1", "title": "Alpha 2", "score": 10, "status": "done", "seq": 1},
    {"id": "r2", "title": "Beta", "score": 5, "status": "todo", "seq": 2},
    {"id": "r3", "title": "Alpha 10", "score": 30, "status": "done", "seq": 3},
    {"id": "r4", "title": "", "score": None, "status": None, "seq": 4},
]


def test_in_memory_filter_preserves_left_to_right_and_or_semantics():
    result = query_records_in_memory(
        FIELDS,
        RECORDS,
        filters=[
            {
                "fieldId": "title",
                "operator": "contains",
                "value": "alpha",
                "logic": "where",
            },
            {
                "fieldId": "score",
                "operator": "gt",
                "value": "20",
                "logic": "or",
            },
            {
                "fieldId": "status",
                "operator": "is",
                "value": "已完成",
                "logic": "and",
            },
        ],
        limit=100,
    )

    # (title contains alpha OR score > 20) AND status == 已完成
    assert [record["id"] for record in result["records"]] == ["r1", "r3"]
    assert result["totalCount"] == 2
    assert result["hasMore"] is False
    assert result["nextOffset"] is None


def test_in_memory_multi_sort_and_natural_text_order():
    result = query_records_in_memory(
        FIELDS,
        RECORDS,
        sorts=[
            {"fieldId": "status", "order": "asc"},
            {"fieldId": "title", "order": "asc"},
        ],
        limit=100,
    )

    ids = [record["id"] for record in result["records"]]
    # Raw select IDs are sorted like the current frontend compareSmartValues.
    assert ids.index("r1") < ids.index("r2")
    # Numeric-aware text comparison keeps Alpha 2 before Alpha 10.
    assert ids.index("r1") < ids.index("r3")
    # Empty status sorts last in ascending order.
    assert ids[-1] == "r4"


def test_select_filter_uses_option_label_like_frontend():
    result = query_records_in_memory(
        FIELDS,
        RECORDS,
        filters=[
            {
                "fieldId": "status",
                "operator": "is",
                "value": "已完成",
                "logic": "where",
            }
        ],
        limit=100,
    )
    assert [record["id"] for record in result["records"]] == ["r1", "r3"]


def test_auto_number_filter_uses_formatted_display_value():
    result = query_records_in_memory(
        FIELDS,
        RECORDS,
        filters=[
            {
                "fieldId": "seq",
                "operator": "equals",
                "value": "Q-002",
                "logic": "where",
            }
        ],
        limit=100,
    )
    assert [record["id"] for record in result["records"]] == ["r2"]


def test_unsupported_filter_is_not_compiled_to_sql():
    expression = compile_filter_expression(
        [
            {
                "fieldId": "seq",
                "operator": "contains",
                "value": "Q-",
                "logic": "where",
            }
        ],
        FIELDS,
    )
    assert expression is None


@pytest.fixture
async def query_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'record-query-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        for index, record in enumerate(RECORDS):
            session.add(
                TableRecord(
                    id=record["id"],
                    table_id="table-query",
                    data={key: value for key, value in record.items() if key != "id"},
                    order_index=index,
                )
            )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_db_query_pushes_safe_filters_and_sorts_to_sql(query_db):
    result = await query_records_db(
        query_db,
        "table-query",
        FIELDS,
        filters=[
            {
                "fieldId": "title",
                "operator": "contains",
                "value": "alpha",
                "logic": "where",
            },
            {
                "fieldId": "score",
                "operator": "gt",
                "value": "8",
                "logic": "and",
            },
        ],
        sorts=[{"fieldId": "score", "order": "desc"}],
        limit=100,
    )

    assert result["executionMode"] == "sql"
    assert result["sqlFilterApplied"] is True
    assert result["sqlSortApplied"] is True
    assert result["totalCount"] == 2
    assert [record["id"] for record in result["records"]] == ["r3", "r1"]


@pytest.mark.asyncio
async def test_db_query_maps_select_label_to_stored_option_id(query_db):
    result = await query_records_db(
        query_db,
        "table-query",
        FIELDS,
        filters=[
            {
                "fieldId": "status",
                "operator": "is",
                "value": "已完成",
                "logic": "where",
            }
        ],
        limit=100,
    )

    assert result["sqlFilterApplied"] is True
    assert [record["id"] for record in result["records"]] == ["r1", "r3"]


@pytest.mark.asyncio
async def test_db_query_falls_back_for_display_formatted_auto_number(query_db):
    result = await query_records_db(
        query_db,
        "table-query",
        FIELDS,
        filters=[
            {
                "fieldId": "seq",
                "operator": "contains",
                "value": "Q-00",
                "logic": "where",
            }
        ],
        limit=100,
    )

    assert result["executionMode"] == "memory"
    assert result["sqlFilterApplied"] is False
    assert [record["id"] for record in result["records"]] == [
        "r1",
        "r2",
        "r3",
        "r4",
    ]


@pytest.mark.asyncio
async def test_db_query_applies_pagination_after_exact_filter_and_sort(query_db):
    result = await query_records_db(
        query_db,
        "table-query",
        FIELDS,
        sorts=[{"fieldId": "score", "order": "desc"}],
        offset=1,
        limit=2,
    )

    assert result["totalCount"] == 4
    assert result["offset"] == 1
    assert result["limit"] == 2
    # Frontend desc comparison puts empty values first, then 30, 10, 5.
    assert [record["id"] for record in result["records"]] == ["r3", "r1"]


@pytest.mark.asyncio
async def test_db_query_pages_default_order_inside_database(query_db):
    result = await query_records_db(
        query_db,
        "table-query",
        FIELDS,
        offset=1,
        limit=2,
    )

    assert result["databasePaged"] is True
    assert result["executionMode"] == "sql"
    assert result["totalCount"] == 4
    assert result["hasMore"] is True
    assert result["nextOffset"] == 3
    assert [record["id"] for record in result["records"]] == ["r2", "r3"]


@pytest.mark.asyncio
async def test_db_query_pages_numeric_sort_inside_database(query_db):
    result = await query_records_db(
        query_db,
        "table-query",
        FIELDS,
        sorts=[{"fieldId": "score", "order": "desc"}],
        offset=1,
        limit=2,
    )

    assert result["databasePaged"] is True
    assert result["totalCount"] == 4
    assert result["nextOffset"] == 3
    assert [record["id"] for record in result["records"]] == ["r3", "r1"]


@pytest.mark.asyncio
async def test_db_query_keeps_natural_text_sort_on_compatibility_path(query_db):
    result = await query_records_db(
        query_db,
        "table-query",
        FIELDS,
        sorts=[{"fieldId": "title", "order": "asc"}],
        offset=0,
        limit=2,
    )

    assert result["databasePaged"] is False
    # Natural ordering must remain Alpha 2 before Alpha 10.
    assert [record["id"] for record in result["records"]] == ["r1", "r3"]
