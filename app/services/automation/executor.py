from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import AutomationEvent, AutomationExecution, AutomationRule
from app.models.change_history import ChangeSet
from app.models.smart_table import TableField, TableRecord
from app.models.workspace_member import WorkspaceMember
from app.services.automation.events import MAX_AUTOMATION_DEPTH
from app.services.automation.repository import get_automation
from app.services.automation.validation import (
    evaluate_conditions,
    required_permission_for_actions,
)
from app.services.collaboration.notifications import (
    publish_notification_rows,
    stage_notification,
)
from app.services.row_permissions import require_record_access
from app.services.smart_table_store import apply_record_patches, create_records_with_data
from app.services.workspace import get_effective_permission_for_item, permission_allows


RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300
TERMINAL_EXECUTION_STATUSES = {"succeeded", "skipped", "failed", "partially_failed"}
RETRYABLE_EXECUTION_STATUSES = {"queued", "retry_scheduled"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _error_code(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, ValueError):
        return "validation_error"
    return "action_error"


def _requires_session_rollback(exc: Exception) -> bool:
    """Return whether a failure may have poisoned the caller transaction.

    Automation permission/validation failures are deliberate business outcomes.
    They happen before a write begins (or after a preceding action has already
    committed) and must not roll back the shared AsyncSession: Session.rollback
    expires every ORM instance owned by the caller, which violates this service
    boundary and causes implicit async reloads. Unknown/infrastructure failures
    keep the conservative rollback behavior.
    """
    return not isinstance(exc, (PermissionError, ValueError))


def _member_ids(value: Any) -> set[int]:
    result: set[int] = set()
    if isinstance(value, list):
        for item in value:
            result.update(_member_ids(item))
        return result
    if isinstance(value, Mapping):
        for key in ("id", "userId", "user_id", "value"):
            candidate = value.get(key)
            if candidate is not None:
                try:
                    result.add(int(candidate))
                except (TypeError, ValueError):
                    pass
                break
        return result
    if value is None or isinstance(value, bool):
        return result
    try:
        result.add(int(value))
    except (TypeError, ValueError):
        pass
    return result


def serialize_execution(execution: AutomationExecution) -> Dict[str, Any]:
    return {
        "id": execution.id,
        "automationId": execution.automation_id,
        "automationVersion": int(execution.automation_version or 1),
        "triggerEventId": execution.trigger_event_id,
        "rootEventId": execution.root_event_id,
        "parentExecutionId": execution.parent_execution_id,
        "recordId": execution.record_id,
        "depth": int(execution.depth or 0),
        "status": execution.status,
        "traceId": execution.trace_id,
        "attempt": int(execution.attempt or 0),
        "actionResults": list(execution.action_results or []),
        "changeSetIds": list(execution.change_set_ids or []),
        "errorCode": execution.error_code,
        "errorMessage": execution.error_message,
        "nextRetryAt": execution.next_retry_at.isoformat() if execution.next_retry_at else None,
        "startedAt": execution.started_at.isoformat() if execution.started_at else None,
        "finishedAt": execution.finished_at.isoformat() if execution.finished_at else None,
        "createdAt": execution.created_at.isoformat() if execution.created_at else None,
        "updatedAt": execution.updated_at.isoformat() if execution.updated_at else None,
    }


async def _get_or_create_execution(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    event: AutomationEvent,
) -> tuple[AutomationExecution, bool]:
    # Freeze the unique-key context before entering a savepoint. A uniqueness
    # race may roll back the nested transaction, and recovery must not depend on
    # ORM attributes that could later be expired by transaction management.
    rule_id = str(rule.id)
    rule_version = int(rule.version or 1)
    event_id = str(event.id)
    root_event_id = str(event.root_event_id) if event.root_event_id else None
    parent_execution_id = (
        str(event.parent_execution_id) if event.parent_execution_id else None
    )
    record_id = str(event.record_id) if event.record_id is not None else None
    event_depth = int(event.depth or 0)

    existing = (
        await db.execute(
            select(AutomationExecution).where(
                AutomationExecution.automation_id == rule_id,
                AutomationExecution.automation_version == rule_version,
                AutomationExecution.trigger_event_id == event_id,
            ).limit(1)
        )
    ).scalars().first()
    if existing is not None:
        return existing, False

    now = utcnow()
    execution = AutomationExecution(
        id=_id("aex"),
        automation_id=rule_id,
        automation_version=rule_version,
        trigger_event_id=event_id,
        root_event_id=root_event_id,
        parent_execution_id=parent_execution_id,
        record_id=record_id,
        depth=event_depth,
        status="queued",
        trace_id=_id("atr"),
        attempt=0,
        action_results=[],
        change_set_ids=[],
        created_at=now,
        updated_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(execution)
            await db.flush()
        await db.commit()
        return execution, True
    except IntegrityError:
        # begin_nested() already rolled back the failed savepoint. Do not roll
        # back the outer transaction here: doing so would expire rule/event ORM
        # instances and can also discard unrelated work owned by the caller.
        existing = (
            await db.execute(
                select(AutomationExecution).where(
                    AutomationExecution.automation_id == rule_id,
                    AutomationExecution.automation_version == rule_version,
                    AutomationExecution.trigger_event_id == event_id,
                ).limit(1)
            )
        ).scalars().first()
        if existing is None:
            raise
        return existing, False


async def _field_map(db: AsyncSession, table_id: str) -> Dict[str, TableField]:
    result = await db.execute(
        select(TableField).where(TableField.table_id == str(table_id))
    )
    return {str(field.id): field for field in result.scalars().all()}


async def _runtime_record(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    event: AutomationEvent,
    run_as_user_id: int,
    table_permission: str,
) -> Optional[TableRecord]:
    if not event.record_id:
        return None
    return await require_record_access(
        db,
        rule.table_id,
        str(event.record_id),
        user_id=int(run_as_user_id),
        table_permission=table_permission,
    )


async def _validate_runtime_permission(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    event: AutomationEvent,
) -> tuple[int, str, Optional[TableRecord]]:
    if rule.run_as_user_id is None:
        raise PermissionError("Automation run-as user no longer exists")
    run_as_user_id = int(rule.run_as_user_id)
    required = required_permission_for_actions(list(rule.actions or []))
    permission = await get_effective_permission_for_item(
        db,
        run_as_user_id,
        rule.table_id,
    )
    if not permission_allows(permission, required):
        raise PermissionError(
            f"Automation run-as user no longer has required '{required}' permission"
        )
    record = await _runtime_record(
        db,
        rule=rule,
        event=event,
        run_as_user_id=run_as_user_id,
        table_permission=str(permission),
    )
    return run_as_user_id, str(permission), record


async def _change_set_ids(db: AsyncSession, trace_id: str) -> List[str]:
    result = await db.execute(
        select(ChangeSet.id)
        .where(ChangeSet.trace_id == str(trace_id))
        .order_by(ChangeSet.created_at.asc(), ChangeSet.id.asc())
    )
    return [str(value) for value in result.scalars().all()]


async def _workspace_recipient_ids(
    db: AsyncSession,
    *,
    workspace_id: str,
    candidate_ids: Iterable[int],
) -> List[int]:
    candidates = sorted({int(value) for value in candidate_ids if int(value) > 0})
    if not candidates:
        return []
    result = await db.execute(
        select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == str(workspace_id),
            WorkspaceMember.user_id.in_(candidates),
        )
    )
    allowed = {int(value) for value in result.scalars().all()}
    return [value for value in candidates if value in allowed]


async def _execute_notify_action(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    execution: AutomationExecution,
    event: AutomationEvent,
    action: Mapping[str, Any],
    record_data: Mapping[str, Any],
    action_index: int,
    actor_user_id: int,
) -> Dict[str, Any]:
    recipients = {int(value) for value in action.get("recipientUserIds") or []}
    recipient_field_id = action.get("recipientFieldId")
    if recipient_field_id:
        recipients.update(_member_ids(record_data.get(str(recipient_field_id))))
    recipients = set(
        await _workspace_recipient_ids(
            db,
            workspace_id=rule.workspace_id,
            candidate_ids=recipients,
        )
    )
    if not recipients:
        raise ValueError("Automation notification has no accessible workspace recipient")

    created_rows = []
    notification_ids: List[str] = []
    for recipient_user_id in sorted(recipients):
        notification, created = await stage_notification(
            db,
            recipient_user_id=recipient_user_id,
            type=str(action.get("notificationType") or "automation"),
            actor_id=actor_user_id,
            workspace_id=rule.workspace_id,
            table_id=rule.table_id,
            record_id=event.record_id,
            comment_id=None,
            event_id=f"{execution.id}:{action_index}:{recipient_user_id}",
            dedupe_key=f"automation:{execution.id}:{action_index}:{recipient_user_id}",
            payload={
                "automationId": rule.id,
                "executionId": execution.id,
                "message": str(action.get("message") or "自动化规则已触发"),
            },
        )
        notification_ids.append(notification.id)
        if created:
            created_rows.append(notification)
    await db.commit()
    await publish_notification_rows(created_rows)
    return {
        "notificationIds": notification_ids,
        "recipientCount": len(recipients),
    }


async def _execute_action(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    execution: AutomationExecution,
    event: AutomationEvent,
    action: Mapping[str, Any],
    action_index: int,
    run_as_user_id: int,
    table_permission: str,
    record: Optional[TableRecord],
) -> Dict[str, Any]:
    action_type = str(action.get("type") or "")
    if action_type == "update_record":
        if record is None or event.record_id is None:
            raise ValueError("update_record requires a record-triggered execution")
        # Re-evaluate row access immediately before every write. A prior action
        # may have changed the member field that controls row visibility.
        record = await require_record_access(
            db,
            rule.table_id,
            str(event.record_id),
            user_id=run_as_user_id,
            table_permission=table_permission,
        )
        fields = dict(action.get("fields") or {})
        patches = [
            {"recordId": str(event.record_id), "fieldId": field_id, "value": value}
            for field_id, value in fields.items()
        ]
        await apply_record_patches(
            db,
            rule.table_id,
            patches,
            user_id=run_as_user_id,
            actor_type="automation",
            source="automation",
            trace_id=execution.trace_id,
        )
        return {
            "recordId": str(event.record_id),
            "changedFieldIds": sorted(fields.keys()),
        }

    if action_type == "create_record":
        created = await create_records_with_data(
            db,
            rule.table_id,
            [dict(action.get("fields") or {})],
            created_by_user_id=run_as_user_id,
            actor_type="automation",
            source="automation",
            trace_id=execution.trace_id,
        )
        return {"recordIds": [str(item.get("id")) for item in created]}

    if action_type == "notify":
        record_data = dict(record.data or {}) if record is not None else dict(event.after_data or {})
        return await _execute_notify_action(
            db,
            rule=rule,
            execution=execution,
            event=event,
            action=action,
            record_data=record_data,
            action_index=action_index,
            actor_user_id=run_as_user_id,
        )

    raise ValueError(f"Unsupported automation action: {action_type}")


async def _notify_final_failure(
    db: AsyncSession,
    *,
    run_as_user_id: Optional[int],
    workspace_id: str,
    table_id: str,
    record_id: Optional[str],
    automation_id: str,
    execution_id: str,
    error_code: Optional[str],
) -> None:
    """Best-effort failure notification using rollback-safe scalar context."""
    if run_as_user_id is None:
        return
    try:
        notification, created = await stage_notification(
            db,
            recipient_user_id=int(run_as_user_id),
            type="automation_failed",
            actor_id=None,
            workspace_id=workspace_id,
            table_id=table_id,
            record_id=record_id,
            comment_id=None,
            event_id=f"automation-failed:{execution_id}",
            dedupe_key=f"automation-failed:{execution_id}",
            payload={
                "automationId": automation_id,
                "executionId": execution_id,
                "errorCode": error_code,
            },
        )
        await db.commit()
        if created:
            await publish_notification_rows([notification])
    except Exception as exc:
        # A logical notification rejection has not dirtied the transaction and
        # must not expire caller ORM state. Unexpected infrastructure/database
        # failures still get the conservative rollback needed for reuse.
        if _requires_session_rollback(exc):
            await db.rollback()


async def execute_rule_for_event(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    event: AutomationEvent,
) -> AutomationExecution:
    execution, created = await _get_or_create_execution(db, rule=rule, event=event)
    if not created and execution.status in TERMINAL_EXECUTION_STATUSES:
        return execution
    if not created and execution.status not in RETRYABLE_EXECUTION_STATUSES:
        return execution

    # Rollback expires ORM state even when expire_on_commit=False. Freeze every
    # value needed to recover the execution before entering code that may roll
    # back, so the exception path never performs implicit async IO via expired
    # attributes (which raises MissingGreenlet under AsyncSession).
    execution_id = str(execution.id)
    max_retries = int(rule.max_retries or 0)
    failure_run_as_user_id = (
        int(rule.run_as_user_id) if rule.run_as_user_id is not None else None
    )
    failure_workspace_id = str(rule.workspace_id)
    failure_table_id = str(rule.table_id)
    failure_record_id = str(event.record_id) if event.record_id is not None else None
    failure_automation_id = str(rule.id)

    now = utcnow()
    execution.status = "running"
    execution.attempt = int(execution.attempt or 0) + 1
    execution.started_at = now
    execution.finished_at = None
    execution.next_retry_at = None
    execution.error_code = None
    execution.error_message = None
    execution.updated_at = now
    await db.commit()

    try:
        if int(event.depth or 0) > MAX_AUTOMATION_DEPTH:
            execution.status = "skipped"
            execution.error_code = "loop_guard"
            execution.error_message = (
                f"Automation chain depth exceeded {MAX_AUTOMATION_DEPTH}"
            )
            execution.finished_at = utcnow()
            execution.updated_at = execution.finished_at
            await db.commit()
            return execution

        if not rule.enabled or rule.deleted_at is not None:
            execution.status = "skipped"
            execution.error_code = "automation_disabled"
            execution.error_message = "Automation is disabled"
            execution.finished_at = utcnow()
            execution.updated_at = execution.finished_at
            await db.commit()
            return execution

        run_as_user_id, table_permission, record = await _validate_runtime_permission(
            db,
            rule=rule,
            event=event,
        )
        fields = await _field_map(db, rule.table_id)
        record_data = (
            dict(record.data or {})
            if record is not None
            else dict(event.after_data or {})
        )
        conditions = dict(rule.conditions or {})
        if conditions.get("items") and not record_data:
            execution.status = "skipped"
            execution.error_code = "condition_record_missing"
            execution.error_message = "Conditions require a record context"
            execution.finished_at = utcnow()
            execution.updated_at = execution.finished_at
            await db.commit()
            return execution
        if not evaluate_conditions(conditions, record_data, fields):
            execution.status = "skipped"
            execution.error_code = "conditions_not_met"
            execution.error_message = None
            execution.finished_at = utcnow()
            execution.updated_at = execution.finished_at
            await db.commit()
            return execution

        existing_results = {
            int(item.get("index")): dict(item)
            for item in list(execution.action_results or [])
            if isinstance(item, Mapping) and item.get("index") is not None
        }
        actions: Sequence[Mapping[str, Any]] = list(rule.actions or [])
        for index, action in enumerate(actions):
            if existing_results.get(index, {}).get("status") == "succeeded":
                continue
            try:
                detail = await _execute_action(
                    db,
                    rule=rule,
                    execution=execution,
                    event=event,
                    action=action,
                    action_index=index,
                    run_as_user_id=run_as_user_id,
                    table_permission=table_permission,
                    record=record,
                )
                existing_results[index] = {
                    "index": index,
                    "type": str(action.get("type") or ""),
                    "status": "succeeded",
                    **detail,
                }
                execution.action_results = [
                    existing_results[key] for key in sorted(existing_results)
                ]
                execution.change_set_ids = await _change_set_ids(db, execution.trace_id)
                execution.updated_at = utcnow()
                await db.commit()
            except Exception as exc:
                existing_results[index] = {
                    "index": index,
                    "type": str(action.get("type") or ""),
                    "status": "failed",
                    "errorCode": _error_code(exc),
                    "errorMessage": str(exc)[:500],
                }
                execution.action_results = [
                    existing_results[key] for key in sorted(existing_results)
                ]
                raise

        execution.status = "succeeded"
        execution.change_set_ids = await _change_set_ids(db, execution.trace_id)
        execution.error_code = None
        execution.error_message = None
        execution.next_retry_at = None
        execution.finished_at = utcnow()
        execution.updated_at = execution.finished_at
        await db.commit()
        return execution

    except Exception as exc:
        # Known business failures do not poison SQLAlchemy's transaction and
        # often occur before any write. Preserve the caller-owned Session and
        # its ORM identity map in those cases. Unknown/infrastructure failures
        # remain fail-safe and reset the transaction before recovery.
        if _requires_session_rollback(exc):
            await db.rollback()
        execution = (
            await db.execute(
                select(AutomationExecution).where(
                    AutomationExecution.id == execution_id
                ).limit(1)
            )
        ).scalars().one()
        succeeded = any(
            isinstance(item, Mapping) and item.get("status") == "succeeded"
            for item in list(execution.action_results or [])
        )
        execution.error_code = _error_code(exc)
        execution.error_message = str(exc)[:1000]
        execution.change_set_ids = await _change_set_ids(db, execution.trace_id)
        if int(execution.attempt or 0) <= max_retries:
            delay_seconds = min(
                RETRY_MAX_SECONDS,
                RETRY_BASE_SECONDS * (2 ** max(0, int(execution.attempt or 1) - 1)),
            )
            execution.status = "retry_scheduled"
            execution.next_retry_at = utcnow() + timedelta(seconds=delay_seconds)
            execution.finished_at = None
        else:
            execution.status = "partially_failed" if succeeded else "failed"
            execution.next_retry_at = None
            execution.finished_at = utcnow()
        execution.updated_at = utcnow()
        is_final_failure = execution.status in {"failed", "partially_failed"}
        notification_error_code = execution.error_code
        await db.commit()
        if is_final_failure:
            await _notify_final_failure(
                db,
                run_as_user_id=failure_run_as_user_id,
                workspace_id=failure_workspace_id,
                table_id=failure_table_id,
                record_id=failure_record_id,
                automation_id=failure_automation_id,
                execution_id=execution_id,
                error_code=notification_error_code,
            )
        # The best-effort notification may commit or, for infrastructure errors,
        # roll back its own work. Refresh the execution before returning so the
        # service result is always safe to serialize.
        await db.refresh(execution)
        return execution


async def preview_rule_for_record(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    record_id: Optional[str] = None,
) -> Dict[str, Any]:
    if rule.run_as_user_id is None:
        raise PermissionError("Automation run-as user no longer exists")
    permission = await get_effective_permission_for_item(
        db,
        int(rule.run_as_user_id),
        rule.table_id,
    )
    required = required_permission_for_actions(list(rule.actions or []))
    if not permission_allows(permission, required):
        raise PermissionError(
            f"Automation run-as user lacks required '{required}' permission"
        )

    record: Optional[TableRecord] = None
    if record_id:
        record = await require_record_access(
            db,
            rule.table_id,
            str(record_id),
            user_id=int(rule.run_as_user_id),
            table_permission=str(permission),
        )
    fields = await _field_map(db, rule.table_id)
    record_data = dict(record.data or {}) if record is not None else {}
    conditions = dict(rule.conditions or {})
    condition_match = (
        evaluate_conditions(conditions, record_data, fields)
        if (record_data or not conditions.get("items"))
        else False
    )
    action_summaries: List[Dict[str, Any]] = []
    for index, action in enumerate(list(rule.actions or [])):
        action_type = str(action.get("type") or "")
        summary: Dict[str, Any] = {"index": index, "type": action_type}
        if action_type in {"update_record", "create_record"}:
            summary["fieldIds"] = sorted(str(key) for key in dict(action.get("fields") or {}).keys())
        elif action_type == "notify":
            summary["notificationType"] = str(action.get("notificationType") or "automation")
            summary["hasRecordRecipients"] = bool(action.get("recipientFieldId"))
            summary["explicitRecipientCount"] = len(action.get("recipientUserIds") or [])
        action_summaries.append(summary)
    return {
        "automationId": rule.id,
        "automationVersion": int(rule.version or 1),
        "recordId": str(record_id) if record_id else None,
        "conditionMatch": condition_match,
        "willExecute": bool(rule.enabled and condition_match),
        "requiredPermission": required,
        "actionSummaries": action_summaries,
    }


async def list_executions(
    db: AsyncSession,
    *,
    user_id: int,
    automation_id: str,
    status: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    await get_automation(
        db,
        user_id=user_id,
        automation_id=automation_id,
        required_permission="read",
    )
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(200, int(limit)))
    query = select(AutomationExecution).where(
        AutomationExecution.automation_id == str(automation_id)
    )
    if status:
        query = query.where(AutomationExecution.status == str(status))
    query = query.order_by(
        AutomationExecution.created_at.desc(), AutomationExecution.id.desc()
    ).offset(safe_offset).limit(safe_limit + 1)
    rows = list((await db.execute(query)).scalars().all())
    has_more = len(rows) > safe_limit
    rows = rows[:safe_limit]
    return {
        "items": [serialize_execution(row) for row in rows],
        "offset": safe_offset,
        "limit": safe_limit,
        "hasMore": has_more,
    }


async def get_execution(
    db: AsyncSession,
    *,
    user_id: int,
    execution_id: str,
) -> AutomationExecution:
    execution = (
        await db.execute(
            select(AutomationExecution).where(
                AutomationExecution.id == str(execution_id)
            ).limit(1)
        )
    ).scalars().first()
    if execution is None:
        raise ValueError("Automation execution not found")
    await get_automation(
        db,
        user_id=user_id,
        automation_id=execution.automation_id,
        required_permission="read",
    )
    return execution


async def retry_execution(
    db: AsyncSession,
    *,
    user_id: int,
    execution_id: str,
) -> AutomationExecution:
    execution = await get_execution(db, user_id=user_id, execution_id=execution_id)
    await get_automation(
        db,
        user_id=user_id,
        automation_id=execution.automation_id,
        required_permission="edit",
    )
    if execution.status not in {"failed", "partially_failed"}:
        raise ValueError("Only failed automation executions can be retried")
    execution.status = "retry_scheduled"
    execution.next_retry_at = utcnow()
    execution.finished_at = None
    execution.error_code = None
    execution.error_message = None
    execution.updated_at = utcnow()
    await db.commit()
    await db.refresh(execution)
    return execution
