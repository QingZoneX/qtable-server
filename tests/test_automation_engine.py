from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.db.base import Base
from app.models.automation import AutomationEvent, AutomationExecution
from app.models.change_history import ChangeSet
from app.models.collaboration import UserNotification
from app.models.smart_table import TableField, TableRecord, WorkspaceItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.automation import (
    AutomationValidationError,
    create_automation,
    process_automation_cycle,
    set_automation_enabled,
    update_automation,
    validate_automation_definition,
)
from app.services.automation.events import queue_synthetic_automation_event
from app.services.automation.executor import execute_rule_for_event
from app.services.smart_table_store import apply_record_patches


@pytest.fixture
async def automation_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'automation-test.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1, email="owner1@example.com", password_hash="test", name="Owner One"),
                User(id=2, email="owner2@example.com", password_hash="test", name="Owner Two"),
                User(id=3, email="viewer@example.com", password_hash="test", name="Viewer"),
                Workspace(id="ws-auto", name="Automation Workspace"),
                WorkspaceMember(user_id=1, workspace_id="ws-auto", role=WorkspaceRole.owner),
                WorkspaceMember(user_id=2, workspace_id="ws-auto", role=WorkspaceRole.owner),
                WorkspaceMember(user_id=3, workspace_id="ws-auto", role=WorkspaceRole.viewer),
                WorkspaceItem(
                    id="root-auto",
                    workspace_id="ws-auto",
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id="table-auto",
                    workspace_id="ws-auto",
                    type="table",
                    name="Tasks",
                    parent_id="root-auto",
                    order_index=0,
                    default_view_id="v1",
                ),
                TableField(id="title", table_id="table-auto", name="Title", type="text", order_index=0),
                TableField(id="status", table_id="table-auto", name="Status", type="text", order_index=1),
                TableField(id="result", table_id="table-auto", name="Result", type="text", order_index=2),
                TableField(id="points", table_id="table-auto", name="Points", type="number", order_index=3),
                TableField(id="due", table_id="table-auto", name="Due", type="date", order_index=4),
                TableField(id="assignee", table_id="table-auto", name="Assignee", type="member", order_index=5),
                TableRecord(
                    id="r1",
                    table_id="table-auto",
                    data={
                        "title": "Alpha",
                        "status": "todo",
                        "result": "",
                        "points": 3,
                        "due": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                        "assignee": [2],
                    },
                    order_index=1,
                    created_by_user_id=1,
                    version=1,
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


async def _rule(
    db,
    *,
    actions,
    trigger=None,
    conditions=None,
    max_retries=0,
    enabled=True,
    user_id=1,
    name="Rule",
):
    return await create_automation(
        db,
        user_id=user_id,
        table_id="table-auto",
        name=name,
        trigger=trigger or {"type": "record.updated", "fieldIds": ["status"]},
        conditions=conditions or {"op": "and", "items": []},
        actions=actions,
        max_retries=max_retries,
        enabled=enabled,
    )


async def _execution_count(db, automation_id: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(AutomationExecution)
                .where(AutomationExecution.automation_id == automation_id)
            )
        ).scalar()
        or 0
    )


@pytest.mark.asyncio
async def test_validation_rejects_type_incompatible_condition(automation_db):
    with pytest.raises(AutomationValidationError):
        await validate_automation_definition(
            automation_db,
            table_id="table-auto",
            trigger={"type": "record.updated", "fieldIds": ["points"]},
            conditions={"fieldId": "points", "operator": "contains", "value": "3"},
            actions=[{"type": "update_record", "fields": {"result": "done"}}],
        )


@pytest.mark.asyncio
async def test_status_change_executes_once_and_writes_audited_changeset(automation_db):
    rule = await _rule(
        automation_db,
        trigger={
            "type": "record.updated",
            "fieldIds": ["status"],
            "from": "todo",
            "to": "review",
        },
        actions=[{"type": "update_record", "fields": {"result": "automated"}}],
    )
    await apply_record_patches(
        automation_db,
        "table-auto",
        [{"recordId": "r1", "fieldId": "status", "value": "review"}],
        user_id=2,
    )

    cycle = await process_automation_cycle(automation_db)
    assert cycle["recordEventsMaterialized"] >= 1
    # A cycle must dispatch the event it just materialized, rather than adding
    # one full worker poll of latency because of a stale time watermark.
    assert cycle["eventsProcessed"] >= 1
    assert await _execution_count(automation_db, rule.id) == 1

    record = (
        await automation_db.execute(
            select(TableRecord).where(TableRecord.table_id == "table-auto", TableRecord.id == "r1")
        )
    ).scalars().one()
    assert record.data["result"] == "automated"

    execution = (
        await automation_db.execute(
            select(AutomationExecution).where(AutomationExecution.automation_id == rule.id)
        )
    ).scalars().one()
    assert execution.status == "succeeded"
    assert len(execution.change_set_ids or []) == 1
    change_set = (
        await automation_db.execute(
            select(ChangeSet).where(ChangeSet.id == execution.change_set_ids[0])
        )
    ).scalars().one()
    assert change_set.actor_type == "automation"
    assert change_set.source == "automation"
    assert change_set.trace_id == execution.trace_id

    await process_automation_cycle(automation_db)
    assert await _execution_count(automation_db, rule.id) == 1


@pytest.mark.asyncio
async def test_disabled_rule_does_not_replay_old_change_after_enable(automation_db):
    rule = await _rule(
        automation_db,
        actions=[{"type": "update_record", "fields": {"result": "should-not-run"}}],
        enabled=False,
    )
    await apply_record_patches(
        automation_db,
        "table-auto",
        [{"recordId": "r1", "fieldId": "status", "value": "review"}],
        user_id=2,
    )
    await process_automation_cycle(automation_db)
    assert await _execution_count(automation_db, rule.id) == 0

    await set_automation_enabled(
        automation_db,
        user_id=1,
        automation_id=rule.id,
        enabled=True,
    )
    await process_automation_cycle(automation_db)
    assert await _execution_count(automation_db, rule.id) == 0


@pytest.mark.asyncio
async def test_lost_runtime_permission_blocks_write(automation_db):
    rule = await _rule(
        automation_db,
        actions=[{"type": "update_record", "fields": {"result": "forbidden"}}],
        max_retries=0,
    )
    membership = (
        await automation_db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == 1,
                WorkspaceMember.workspace_id == "ws-auto",
            )
        )
    ).scalars().one()
    await automation_db.delete(membership)
    await automation_db.commit()

    await apply_record_patches(
        automation_db,
        "table-auto",
        [{"recordId": "r1", "fieldId": "status", "value": "review"}],
        user_id=2,
    )
    await process_automation_cycle(automation_db)

    execution = (
        await automation_db.execute(
            select(AutomationExecution).where(AutomationExecution.automation_id == rule.id)
        )
    ).scalars().one()
    assert execution.status == "failed"
    assert execution.error_code == "permission_denied"
    record = (
        await automation_db.execute(
            select(TableRecord).where(TableRecord.table_id == "table-auto", TableRecord.id == "r1")
        )
    ).scalars().one()
    assert record.data["result"] == ""


@pytest.mark.asyncio
async def test_retry_does_not_repeat_successful_create_action(automation_db):
    rule = await _rule(
        automation_db,
        actions=[
            {"type": "create_record", "fields": {"title": "Generated", "status": "todo"}},
            {
                "type": "notify",
                "recipientUserIds": [999999],
                "notificationType": "automation",
                "message": "This recipient is intentionally unavailable",
            },
        ],
        max_retries=1,
    )
    await apply_record_patches(
        automation_db,
        "table-auto",
        [{"recordId": "r1", "fieldId": "status", "value": "review"}],
        user_id=2,
    )
    await process_automation_cycle(automation_db)

    execution = (
        await automation_db.execute(
            select(AutomationExecution).where(AutomationExecution.automation_id == rule.id)
        )
    ).scalars().one()
    assert execution.status == "retry_scheduled"
    first_results = {item["index"]: item for item in execution.action_results}
    assert first_results[0]["status"] == "succeeded"
    assert first_results[1]["status"] == "failed"

    generated_before = int(
        (
            await automation_db.execute(
                select(func.count())
                .select_from(TableRecord)
                .where(TableRecord.table_id == "table-auto")
            )
        ).scalar()
    )
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await process_automation_cycle(automation_db, now=future)
    generated_after = int(
        (
            await automation_db.execute(
                select(func.count())
                .select_from(TableRecord)
                .where(TableRecord.table_id == "table-auto")
            )
        ).scalar()
    )
    assert generated_after == generated_before
    await automation_db.refresh(execution)
    assert execution.status == "partially_failed"
    assert execution.attempt == 2


@pytest.mark.asyncio
async def test_retry_fails_closed_when_rule_version_changed(automation_db):
    rule = await _rule(
        automation_db,
        actions=[
            {
                "type": "notify",
                "recipientUserIds": [999999],
                "notificationType": "automation",
                "message": "force retry",
            }
        ],
        max_retries=1,
    )
    await apply_record_patches(
        automation_db,
        "table-auto",
        [{"recordId": "r1", "fieldId": "status", "value": "review"}],
        user_id=2,
    )
    await process_automation_cycle(automation_db)
    execution = (
        await automation_db.execute(
            select(AutomationExecution).where(AutomationExecution.automation_id == rule.id)
        )
    ).scalars().one()
    assert execution.status == "retry_scheduled"
    assert execution.automation_version == 1

    rule = await update_automation(
        automation_db,
        user_id=1,
        automation_id=rule.id,
        actions=[{"type": "update_record", "fields": {"result": "new-version"}}],
    )
    assert rule.version == 2

    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await process_automation_cycle(automation_db, now=future)
    await automation_db.refresh(execution)
    assert execution.status == "failed"
    assert execution.error_code == "automation_version_changed"
    # Do not silently create a version-2 execution from the old trigger event.
    assert await _execution_count(automation_db, rule.id) == 1


@pytest.mark.asyncio
async def test_due_date_scan_is_deterministic_and_sends_notification(automation_db):
    rule = await _rule(
        automation_db,
        trigger={
            "type": "due_date",
            "fieldId": "due",
            "offsetMinutes": 3 * 24 * 60,
            "scanIntervalMinutes": 5,
        },
        conditions={"fieldId": "status", "operator": "equals", "value": "todo"},
        actions=[
            {
                "type": "notify",
                "recipientUserIds": [2],
                "notificationType": "automation",
                "message": "任务将在三天内到期",
            }
        ],
        name="Due reminder",
    )
    now = datetime.now(timezone.utc)
    cycle = await process_automation_cycle(automation_db, now=now)
    assert cycle["schedule"]["dueEvents"] == 1
    assert cycle["eventsProcessed"] >= 1

    notifications = list(
        (
            await automation_db.execute(
                select(UserNotification).where(
                    UserNotification.recipient_user_id == 2,
                    UserNotification.type == "automation",
                )
            )
        ).scalars().all()
    )
    assert len(notifications) == 1
    execution_count = await _execution_count(automation_db, rule.id)
    assert execution_count == 1

    # Force another due scan; deterministic event and execution identities must
    # keep the reminder idempotent.
    rule.next_run_at = now
    await automation_db.commit()
    await process_automation_cycle(automation_db, now=now + timedelta(seconds=1))
    notifications = list(
        (
            await automation_db.execute(
                select(UserNotification).where(
                    UserNotification.recipient_user_id == 2,
                    UserNotification.type == "automation",
                )
            )
        ).scalars().all()
    )
    assert len(notifications) == 1
    assert await _execution_count(automation_db, rule.id) == 1


@pytest.mark.asyncio
async def test_loop_guard_skips_over_depth_event(automation_db):
    rule = await _rule(
        automation_db,
        trigger={"type": "manual"},
        actions=[{"type": "update_record", "fields": {"result": "loop"}}],
    )
    event = await queue_synthetic_automation_event(
        automation_db,
        event_id="evt-depth-guard",
        workspace_id="ws-auto",
        table_id="table-auto",
        target_automation_id=rule.id,
        event_type="manual",
        record_id="r1",
        after_data={"title": "Alpha"},
    )
    event.depth = 9
    await automation_db.commit()
    execution = await execute_rule_for_event(automation_db, rule=rule, event=event)
    assert execution.status == "skipped"
    assert execution.error_code == "loop_guard"


def test_automation_schema_contract():
    schema_text = str(schema)
    for field_name in (
        "automations",
        "automation(",
        "automationPreview",
        "automationExecutions",
        "automationExecution",
        "validateAutomation",
        "createAutomation",
        "updateAutomation",
        "setAutomationEnabled",
        "deleteAutomation",
        "runAutomation",
        "retryAutomationExecution",
    ):
        assert field_name in schema_text
