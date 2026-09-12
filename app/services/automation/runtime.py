from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import AutomationEvent, AutomationExecution, AutomationRule
from app.services.automation.events import (
    materialize_record_events_from_changesets,
    queue_synthetic_automation_event,
    trigger_matches,
)
from app.services.automation.executor import (
    execute_rule_for_event,
    get_execution,
    list_executions,
    preview_rule_for_record,
    retry_execution,
    serialize_execution,
)
from app.services.automation.repository import get_automation
from app.services.automation.scheduler import scan_due_automations


EVENT_LEASE_SECONDS = 300
RETRY_LEASE_SECONDS = 300


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _claim_events(
    db: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> List[AutomationEvent]:
    safe_limit = max(1, min(500, int(limit)))
    result = await db.execute(
        select(AutomationEvent)
        .where(
            or_(
                (
                    (AutomationEvent.status == "queued")
                    & (AutomationEvent.available_at <= now)
                ),
                # A worker may die after claiming. `available_at` doubles as a
                # processing lease deadline so another worker can recover it.
                (
                    (AutomationEvent.status == "processing")
                    & (AutomationEvent.available_at <= now)
                ),
            )
        )
        .order_by(AutomationEvent.available_at.asc(), AutomationEvent.created_at.asc())
        .limit(safe_limit)
        .with_for_update(skip_locked=True)
    )
    events = list(result.scalars().all())
    lease_until = now + timedelta(seconds=EVENT_LEASE_SECONDS)
    for event in events:
        event.status = "processing"
        event.available_at = lease_until
        event.error_code = None
        event.error_message = None
    if events:
        await db.commit()
    return events


async def _rules_for_event(
    db: AsyncSession,
    event: AutomationEvent,
) -> List[AutomationRule]:
    query = select(AutomationRule).where(
        AutomationRule.table_id == event.table_id,
        AutomationRule.enabled.is_(True),
        AutomationRule.deleted_at.is_(None),
    )
    # Never replay a historical record change into a rule that did not exist
    # when that change occurred. This matters when an upgraded deployment first
    # materializes an existing ChangeSet backlog.
    if event.created_at is not None:
        query = query.where(AutomationRule.created_at <= event.created_at)
    if event.target_automation_id:
        query = query.where(AutomationRule.id == event.target_automation_id)
    rules = list((await db.execute(query.order_by(AutomationRule.id.asc()))).scalars().all())
    return [rule for rule in rules if trigger_matches(rule, event)]


async def _persist_failed_action_result(
    db: AsyncSession,
    *,
    actions: Sequence[Mapping[str, Any]],
    execution: AutomationExecution,
) -> AutomationExecution:
    """Ensure execution history points at the step that failed.

    The caller passes a plain action snapshot instead of an AutomationRule ORM
    object. Executor recovery may roll back the session, which expires every ORM
    instance loaded in that transaction; scalar/list snapshots remain safe.
    """
    if execution.status not in {"retry_scheduled", "failed", "partially_failed"}:
        return execution
    if execution.error_code == "permission_denied":
        return execution
    results = {
        int(item.get("index")): dict(item)
        for item in list(execution.action_results or [])
        if isinstance(item, Mapping) and item.get("index") is not None
    }
    action_list = list(actions)
    pending_index = next(
        (
            index
            for index in range(len(action_list))
            if results.get(index, {}).get("status") != "succeeded"
        ),
        None,
    )
    if pending_index is None:
        return execution
    action = action_list[pending_index]
    results[pending_index] = {
        "index": pending_index,
        "type": str(action.get("type") or ""),
        "status": "failed",
        "errorCode": execution.error_code or "action_error",
        "errorMessage": str(execution.error_message or "Automation action failed")[:500],
    }
    execution.action_results = [results[key] for key in sorted(results)]
    execution.updated_at = utcnow()
    await db.commit()
    await db.refresh(execution)
    return execution


async def _process_event(
    db: AsyncSession,
    event: AutomationEvent,
) -> Dict[str, Any]:
    # Keep only scalar identities across executor calls. An action failure may
    # rollback the shared AsyncSession and expire all ORM rows loaded earlier in
    # this dispatch, including the event and sibling rules.
    event_id = str(event.id)
    try:
        rules = await _rules_for_event(db, event)
        matched_rules = [
            (str(rule.id), [dict(action) for action in list(rule.actions or [])])
            for rule in rules
        ]
        executions: List[Dict[str, Any]] = []
        for rule_id, action_snapshot in matched_rules:
            current_rule = (
                await db.execute(
                    select(AutomationRule).where(
                        AutomationRule.id == rule_id
                    ).limit(1)
                )
            ).scalars().first()
            current_event = (
                await db.execute(
                    select(AutomationEvent).where(
                        AutomationEvent.id == event_id
                    ).limit(1)
                )
            ).scalars().first()
            if current_rule is None or current_event is None:
                raise RuntimeError("Automation dispatch context disappeared")
            execution = await execute_rule_for_event(
                db,
                rule=current_rule,
                event=current_event,
            )
            execution = await _persist_failed_action_result(
                db,
                actions=action_snapshot,
                execution=execution,
            )
            executions.append(serialize_execution(execution))
        current = (
            await db.execute(
                select(AutomationEvent).where(
                    AutomationEvent.id == event_id
                ).limit(1)
            )
        ).scalars().one()
        current.status = "processed"
        current.processed_at = utcnow()
        current.available_at = current.processed_at
        current.error_code = None
        current.error_message = None
        await db.commit()
        return {
            "eventId": event_id,
            "matchedRules": len(matched_rules),
            "executions": executions,
        }
    except Exception as exc:
        await db.rollback()
        current = (
            await db.execute(
                select(AutomationEvent).where(
                    AutomationEvent.id == event_id
                ).limit(1)
            )
        ).scalars().first()
        if current is not None:
            # Dispatch failures are infrastructure/orchestration failures rather
            # than rule failures. Re-queue with a bounded delay; action failures
            # themselves are already captured by AutomationExecution retries.
            current.status = "queued"
            current.error_code = "dispatch_error"
            current.error_message = str(exc)[:1000]
            current.processed_at = None
            current.available_at = utcnow() + timedelta(seconds=30)
            await db.commit()
        return {
            "eventId": event_id,
            "matchedRules": 0,
            "executions": [],
            "error": str(exc),
        }


async def _fail_retry_context(
    db: AsyncSession,
    *,
    execution_id: str,
    now: datetime,
    error_code: str,
    error_message: str,
) -> None:
    execution = (
        await db.execute(
            select(AutomationExecution).where(
                AutomationExecution.id == execution_id
            ).limit(1)
        )
    ).scalars().one()
    execution.status = "failed"
    execution.error_code = error_code
    execution.error_message = error_message[:1000]
    execution.finished_at = now
    execution.next_retry_at = None
    execution.updated_at = now
    await db.commit()


async def _process_due_retries(
    db: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> int:
    safe_limit = max(1, min(500, int(limit)))
    result = await db.execute(
        select(AutomationExecution)
        .where(
            AutomationExecution.status == "retry_scheduled",
            AutomationExecution.next_retry_at.is_not(None),
            AutomationExecution.next_retry_at <= now,
        )
        .order_by(
            AutomationExecution.next_retry_at.asc(),
            AutomationExecution.created_at.asc(),
        )
        .limit(safe_limit)
        .with_for_update(skip_locked=True)
    )
    executions = list(result.scalars().all())
    retry_claims = [
        (
            str(execution.id),
            str(execution.automation_id),
            str(execution.trigger_event_id),
            int(execution.automation_version or 1),
        )
        for execution in executions
    ]
    if executions:
        # Claim the whole batch before executing any action. The future retry
        # timestamp is a durable lease: concurrent workers cannot select these
        # rows, while a crashed worker naturally releases them when it expires.
        lease_until = now + timedelta(seconds=RETRY_LEASE_SECONDS)
        for execution in executions:
            execution.next_retry_at = lease_until
            execution.updated_at = now
        await db.commit()

    processed = 0
    for execution_id, automation_id, trigger_event_id, automation_version in retry_claims:
        rule = (
            await db.execute(
                select(AutomationRule).where(
                    AutomationRule.id == automation_id
                ).limit(1)
            )
        ).scalars().first()
        event = (
            await db.execute(
                select(AutomationEvent).where(
                    AutomationEvent.id == trigger_event_id
                ).limit(1)
            )
        ).scalars().first()
        if rule is None or event is None:
            await _fail_retry_context(
                db,
                execution_id=execution_id,
                now=now,
                error_code="retry_context_missing",
                error_message="Automation rule or trigger event is missing",
            )
            processed += 1
            continue
        if int(rule.version or 1) != automation_version:
            # Executions do not snapshot full rule definitions. Retrying an old
            # failure under a newer rule would violate execution/version audit
            # semantics, so terminate it and require a fresh event/manual run.
            await _fail_retry_context(
                db,
                execution_id=execution_id,
                now=now,
                error_code="automation_version_changed",
                error_message=(
                    "Automation changed after this execution; retry requires a fresh run"
                ),
            )
            processed += 1
            continue
        action_snapshot = [dict(action) for action in list(rule.actions or [])]
        execution = await execute_rule_for_event(db, rule=rule, event=event)
        await _persist_failed_action_result(
            db,
            actions=action_snapshot,
            execution=execution,
        )
        processed += 1
    return processed


async def process_automation_cycle(
    db: AsyncSession,
    *,
    event_limit: int = 50,
    retry_limit: int = 50,
    schedule_limit: int = 100,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = _as_utc(now or utcnow())
    materialized = await materialize_record_events_from_changesets(
        db,
        limit=max(event_limit * 4, 100),
    )
    schedule = await scan_due_automations(db, now=current, limit=schedule_limit)
    retries = await _process_due_retries(db, now=current, limit=retry_limit)

    # Materialization and scheduling happen after `current` is captured. Their
    # newly-created events therefore have `available_at` a few microseconds
    # later. Claim against a fresh watermark so one worker cycle can dispatch
    # the work it just persisted instead of always waiting for the next poll.
    claim_now = max(current, utcnow())
    events = await _claim_events(db, now=claim_now, limit=event_limit)
    event_results = []
    for event in events:
        event_results.append(await _process_event(db, event))
    return {
        "recordEventsMaterialized": materialized,
        "schedule": schedule,
        "retriesProcessed": retries,
        "eventsProcessed": len(event_results),
        "eventResults": event_results,
    }


async def preview_automation(
    db: AsyncSession,
    *,
    user_id: int,
    automation_id: str,
    record_id: Optional[str] = None,
) -> Dict[str, Any]:
    rule = await get_automation(
        db,
        user_id=user_id,
        automation_id=automation_id,
        required_permission="read",
    )
    return await preview_rule_for_record(db, rule=rule, record_id=record_id)


async def execute_manual_automation(
    db: AsyncSession,
    *,
    user_id: int,
    automation_id: str,
    record_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Explicitly execute one rule for testing/manual operations.

    This bypasses trigger *matching*, not validation or permissions. The runtime
    still executes under the rule's stored run-as identity and rechecks table /
    row access immediately before each action.
    """
    rule = await get_automation(
        db,
        user_id=user_id,
        automation_id=automation_id,
        required_permission="edit",
    )
    action_snapshot = [dict(action) for action in list(rule.actions or [])]
    after_data = None
    if record_id:
        from app.models.smart_table import TableRecord

        record = (
            await db.execute(
                select(TableRecord).where(
                    TableRecord.table_id == rule.table_id,
                    TableRecord.id == str(record_id),
                ).limit(1)
            )
        ).scalars().first()
        if record is not None:
            # Visibility is rechecked by executor; this snapshot is not returned
            # to the caller and therefore cannot leak a hidden record.
            after_data = dict(record.data or {})

    event_id = f"evt_manual_{uuid.uuid4().hex}"
    event = await queue_synthetic_automation_event(
        db,
        event_id=event_id,
        workspace_id=rule.workspace_id,
        table_id=rule.table_id,
        target_automation_id=rule.id,
        event_type="manual",
        record_id=record_id,
        after_data=after_data,
        actor_user_id=int(user_id),
        source="automation_manual",
    )
    await db.commit()
    execution = await execute_rule_for_event(db, rule=rule, event=event)
    execution = await _persist_failed_action_result(
        db,
        actions=action_snapshot,
        execution=execution,
    )
    current_event = (
        await db.execute(
            select(AutomationEvent).where(
                AutomationEvent.id == event_id
            ).limit(1)
        )
    ).scalars().one()
    current_event.status = "processed"
    current_event.processed_at = utcnow()
    current_event.available_at = current_event.processed_at
    await db.commit()
    return serialize_execution(execution)


async def list_automation_executions(
    db: AsyncSession,
    *,
    user_id: int,
    automation_id: str,
    status: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    return await list_executions(
        db,
        user_id=user_id,
        automation_id=automation_id,
        status=status,
        offset=offset,
        limit=limit,
    )


async def get_automation_execution(
    db: AsyncSession,
    *,
    user_id: int,
    execution_id: str,
) -> Dict[str, Any]:
    execution = await get_execution(
        db,
        user_id=user_id,
        execution_id=execution_id,
    )
    return serialize_execution(execution)


async def retry_automation_execution(
    db: AsyncSession,
    *,
    user_id: int,
    execution_id: str,
) -> Dict[str, Any]:
    execution = await retry_execution(
        db,
        user_id=user_id,
        execution_id=execution_id,
    )
    return serialize_execution(execution)
