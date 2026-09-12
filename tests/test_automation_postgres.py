from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal, engine
from app.models.automation import AutomationExecution
from app.models.change_history import ChangeSet
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.automation import create_automation, process_automation_cycle
from app.services.smart_table_store import apply_record_patches


pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL-specific automation integration contract",
)


@pytest.mark.asyncio
async def test_automation_postgresql_event_lock_json_changeset_and_idempotency():
    user_id = 949001
    actor_id = 949002
    workspace_id = "ws-automation-pg"
    table_id = "tbl-automation-pg"

    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                User(
                    id=user_id,
                    email="automation-pg@example.test",
                    name="Automation PG",
                    password_hash="!test",
                ),
                User(
                    id=actor_id,
                    email="automation-pg-actor@example.test",
                    name="Automation PG Actor",
                    password_hash="!test",
                ),
                Workspace(id=workspace_id, name="Automation PostgreSQL"),
                WorkspaceItem(
                    id="root-automation-pg",
                    workspace_id=workspace_id,
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id=table_id,
                    workspace_id=workspace_id,
                    type="table",
                    name="Automation Tasks",
                    parent_id="root-automation-pg",
                    order_index=1,
                    default_view_id="v1",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.owner,
                ),
                WorkspaceMember(
                    user_id=actor_id,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                TableField(
                    id="title",
                    table_id=table_id,
                    name="Title",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id=table_id,
                    name="Status",
                    type="text",
                    order_index=1,
                ),
                TableField(
                    id="result",
                    table_id=table_id,
                    name="Result",
                    type="text",
                    order_index=2,
                ),
                TableRecord(
                    id="pg-auto-r1",
                    table_id=table_id,
                    data={"title": "PG task", "status": "todo", "result": ""},
                    order_index=1,
                    created_by_user_id=actor_id,
                    version=1,
                ),
            ]
        )
        await session.commit()

        rule = await create_automation(
            session,
            user_id=user_id,
            table_id=table_id,
            name="PostgreSQL status rule",
            trigger={
                "type": "record.updated",
                "fieldIds": ["status"],
                "from": "todo",
                "to": "review",
            },
            conditions={"op": "and", "items": []},
            actions=[
                {"type": "update_record", "fields": {"result": "postgres-ok"}}
            ],
            max_retries=0,
            enabled=True,
        )
        await apply_record_patches(
            session,
            table_id,
            [
                {
                    "recordId": "pg-auto-r1",
                    "fieldId": "status",
                    "value": "review",
                }
            ],
            user_id=actor_id,
            source="postgres_contract",
        )

        cycle = await process_automation_cycle(session, event_limit=500)
        assert cycle["recordEventsMaterialized"] >= 1
        assert cycle["eventsProcessed"] >= 1

        execution = (
            await session.execute(
                select(AutomationExecution).where(
                    AutomationExecution.automation_id == rule.id
                )
            )
        ).scalars().one()
        assert execution.status == "succeeded"
        assert execution.automation_version == 1
        assert len(execution.change_set_ids or []) == 1

        record = (
            await session.execute(
                select(TableRecord).where(
                    TableRecord.table_id == table_id,
                    TableRecord.id == "pg-auto-r1",
                )
            )
        ).scalars().one()
        assert record.data == {
            "title": "PG task",
            "status": "review",
            "result": "postgres-ok",
        }

        change_set = (
            await session.execute(
                select(ChangeSet).where(ChangeSet.id == execution.change_set_ids[0])
            )
        ).scalars().one()
        assert change_set.actor_type == "automation"
        assert change_set.source == "automation"
        assert change_set.trace_id == execution.trace_id

        # Re-scanning committed ChangeItems and the event queue is idempotent.
        await process_automation_cycle(session, event_limit=500)
        execution_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AutomationExecution)
                    .where(AutomationExecution.automation_id == rule.id)
                )
            ).scalar()
            or 0
        )
        assert execution_count == 1
