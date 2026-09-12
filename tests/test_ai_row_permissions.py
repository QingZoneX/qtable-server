from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.ai import _load_table_snapshot
from app.context_engine.builder import ContextBuilder
from app.core.config import settings
from app.db.base import Base
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.row_permissions import set_row_permission_policy
from app.services.task_split import TaskSplitService
from app.services.estimate_workload import EstimateWorkloadService
from app.services.structured_output import StructuredOutputService
from app.skills.runtime import (
    CreateRecordInput,
    DescribeTableInput,
    SkillExecutionContext,
    handle_create_record,
    handle_describe_table,
)


@pytest.fixture
async def ai_row_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ai-row-permissions.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        alice = User(
            id=401,
            email="alice-ai-row@example.test",
            name="Alice",
            password_hash="!test",
        )
        bob = User(
            id=402,
            email="bob-ai-row@example.test",
            name="Bob",
            password_hash="!test",
        )
        outsider = User(
            id=403,
            email="outsider-ai-row@example.test",
            name="Outsider",
            password_hash="!test",
        )
        workspace = Workspace(id="ws-ai-row", name="AI Row Scope")
        table = WorkspaceItem(
            id="table-ai-row",
            workspace_id=workspace.id,
            type="table",
            name="AI Tasks",
            parent_id=None,
            order_index=0,
        )
        session.add_all(
            [
                alice,
                bob,
                outsider,
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
                table,
                TableField(
                    id="title",
                    table_id=table.id,
                    name="标题",
                    type="text",
                    order_index=0,
                ),
                TableRecord(
                    id="alice-row",
                    table_id=table.id,
                    data={"title": "Alice visible"},
                    order_index=1,
                    created_by_user_id=alice.id,
                ),
                TableRecord(
                    id="bob-row",
                    table_id=table.id,
                    data={"title": "Bob hidden"},
                    order_index=2,
                    created_by_user_id=bob.id,
                ),
            ]
        )
        await session.commit()
        await set_row_permission_policy(session, table.id, mode="creator")
        yield session, alice, bob, outsider, table

    await engine.dispose()


@pytest.mark.asyncio
async def test_ai_fallback_snapshot_only_contains_visible_rows(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    records, fields = await _load_table_snapshot(
        session,
        [table.id],
        user_id=alice.id,
    )

    assert [record["id"] for record in records] == ["alice-row"]
    assert all(record["_table_id"] == table.id for record in records)
    assert [field["id"] for field in fields] == ["title"]
    assert fields[0]["_table_id"] == table.id


@pytest.mark.asyncio
async def test_ai_fallback_snapshot_rejects_table_without_read_access(
    ai_row_db,
    monkeypatch,
):
    session, _, _, outsider, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    with pytest.raises(PermissionError, match="No access to target table"):
        await _load_table_snapshot(
            session,
            [table.id],
            user_id=outsider.id,
        )


@pytest.mark.asyncio
async def test_context_builder_record_count_uses_visible_row_scope(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    context = await ContextBuilder()._build_table_context(
        session,
        [table.id],
        alice.id,
    )

    assert context.id == table.id
    assert context.record_count == 1
    assert context.field_count == 1


@pytest.mark.asyncio
async def test_context_builder_manager_still_counts_all_rows(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    membership = await session.get(
        WorkspaceMember,
        {"user_id": alice.id, "workspace_id": table.workspace_id},
    )
    membership.role = WorkspaceRole.owner
    await session.commit()

    context = await ContextBuilder()._build_table_context(
        session,
        [table.id],
        alice.id,
    )

    assert context.record_count == 2


@pytest.mark.asyncio
async def test_context_builder_rejects_unauthorized_table_metadata(
    ai_row_db,
    monkeypatch,
):
    session, _, _, outsider, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    with pytest.raises(PermissionError, match="No access to target table"):
        await ContextBuilder()._build_table_context(
            session,
            [table.id],
            outsider.id,
        )


@pytest.mark.asyncio
async def test_skill_describe_table_uses_visible_rows_only(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    context = SkillExecutionContext(
        context=SimpleNamespace(
            user_id=alice.id,
            dry_run=False,
            confirmed=True,
            timezone="Asia/Shanghai",
        ),
        db=session,
    )

    result = await handle_describe_table(
        context,
        DescribeTableInput(tableId=table.id, includeSampleRows=True, sampleLimit=10),
    )

    assert result["recordCount"] == 1
    assert [row["id"] for row in result["sampleRows"]] == ["alice-row"]


@pytest.mark.asyncio
async def test_skill_create_record_persists_authenticated_creator(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    context = SkillExecutionContext(
        context=SimpleNamespace(
            user_id=alice.id,
            dry_run=False,
            confirmed=True,
            timezone="Asia/Shanghai",
        ),
        db=session,
    )

    result = await handle_create_record(
        context,
        CreateRecordInput(
            tableId=table.id,
            values={"title": "Created by AI skill"},
        ),
    )

    record = (
        await session.execute(
            select(TableRecord).where(TableRecord.id == result["recordId"])
        )
    ).scalars().one()
    assert record.created_by_user_id == alice.id


@pytest.mark.asyncio
async def test_task_split_table_context_uses_visible_rows_only(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    result = await TaskSplitService()._load_table_context(
        session,
        table_ids=[table.id],
        sample_limit=10,
        user_id=alice.id,
    )

    assert len(result.tables) == 1
    assert result.tables[0].record_count == 1
    assert [row["id"] for row in result.tables[0].sample_rows] == ["alice-row"]


@pytest.mark.asyncio
async def test_task_split_table_context_rejects_unauthorized_table(
    ai_row_db,
    monkeypatch,
):
    session, _, _, outsider, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    with pytest.raises(PermissionError, match="No access to target table"):
        await TaskSplitService()._load_table_context(
            session,
            table_ids=[table.id],
            sample_limit=5,
            user_id=outsider.id,
        )


@pytest.mark.asyncio
async def test_workload_estimation_table_context_uses_visible_rows_only(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    result = await EstimateWorkloadService()._load_table_context(
        session,
        table_ids=[table.id],
        sample_limit=10,
        user_id=alice.id,
    )

    assert result.tables[0].record_count == 1
    assert [row["id"] for row in result.tables[0].sample_rows] == ["alice-row"]


@pytest.mark.asyncio
async def test_structured_output_table_context_uses_visible_rows_only(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, table = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    result = await StructuredOutputService()._load_table_context(
        session,
        table_ids=[table.id],
        sample_limit=10,
        user_id=alice.id,
    )

    assert result.tables[0].record_count == 1
    assert [row["id"] for row in result.tables[0].sample_rows] == ["alice-row"]


@pytest.mark.asyncio
async def test_structured_output_db_context_does_not_fallback_to_default_table(
    ai_row_db,
    monkeypatch,
):
    session, alice, _, _, _ = ai_row_db
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")

    result = await StructuredOutputService()._load_table_context(
        session,
        table_ids=[],
        sample_limit=10,
        user_id=alice.id,
    )

    assert result.tables == []
