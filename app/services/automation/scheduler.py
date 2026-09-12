from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import AutomationRule
from app.models.smart_table import TableRecord
from app.services.automation.events import queue_synthetic_automation_event
from app.services.automation.validation import parse_datetime_value, required_permission_for_actions
from app.services.row_permissions import get_row_permission_policy, record_is_visible
from app.services.workspace import get_effective_permission_for_item, permission_allows


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _due_value_utc(value: Any, timezone_name: str) -> Optional[datetime]:
    parsed = parse_datetime_value(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    try:
        local_zone = ZoneInfo(str(timezone_name or "UTC"))
    except ZoneInfoNotFoundError:
        # Persisted rules should have passed validation; malformed legacy rows
        # fail closed instead of silently interpreting local wall time as UTC.
        return None
    return parsed.replace(tzinfo=local_zone).astimezone(timezone.utc)


def _event_id(*parts: Any) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return f"evt_sched_{digest[:96]}"


def _advance_next_run(rule: AutomationRule, now: datetime) -> datetime:
    trigger = dict(rule.trigger or {})
    trigger_type = str(trigger.get("type") or "")
    if trigger_type == "scheduled":
        interval = max(1, int(trigger.get("intervalMinutes") or 1))
    elif trigger_type == "due_date":
        interval = max(1, int(trigger.get("scanIntervalMinutes") or 5))
    else:
        interval = 60
    return now + timedelta(minutes=interval)


async def _runtime_permission(
    db: AsyncSession,
    rule: AutomationRule,
) -> Optional[str]:
    if rule.run_as_user_id is None:
        return None
    permission = await get_effective_permission_for_item(
        db,
        int(rule.run_as_user_id),
        rule.table_id,
    )
    required = required_permission_for_actions(list(rule.actions or []))
    if not permission_allows(permission, required):
        return None
    return str(permission)


async def _queue_scheduled_rule(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    scheduled_for: datetime,
) -> int:
    event_id = _event_id(
        "scheduled",
        rule.id,
        int(rule.version or 1),
        _aware_utc(scheduled_for).isoformat(),
    )
    await queue_synthetic_automation_event(
        db,
        event_id=event_id,
        workspace_id=rule.workspace_id,
        table_id=rule.table_id,
        target_automation_id=rule.id,
        event_type="scheduled",
    )
    return 1


async def _queue_due_rule(
    db: AsyncSession,
    *,
    rule: AutomationRule,
    now: datetime,
) -> int:
    trigger = dict(rule.trigger or {})
    field_id = str(trigger.get("fieldId") or "")
    if not field_id or rule.run_as_user_id is None:
        return 0
    try:
        offset_minutes = max(0, int(trigger.get("offsetMinutes") or 0))
    except (TypeError, ValueError):
        return 0

    table_permission = await _runtime_permission(db, rule)
    if table_permission is None:
        # Runtime permission recheck will also fail any already-queued event. For
        # scans, avoid even materializing record references after access is lost.
        return 0
    policy = await get_row_permission_policy(db, rule.table_id)
    rows = list(
        (
            await db.execute(
                select(TableRecord).where(TableRecord.table_id == rule.table_id)
            )
        ).scalars().all()
    )

    queued = 0
    now_utc = _aware_utc(now)
    for record in rows:
        data = dict(record.data or {})
        if not record_is_visible(
            policy=policy,
            user_id=int(rule.run_as_user_id),
            table_permission=table_permission,
            created_by_user_id=record.created_by_user_id,
            data=data,
        ):
            continue
        due_utc = _due_value_utc(data.get(field_id), rule.timezone or "UTC")
        if due_utc is None:
            continue
        target_at = due_utc - timedelta(minutes=offset_minutes)
        # Catch up after worker downtime while the due time is still meaningful,
        # but do not emit a new pre-due reminder after the record is already late.
        if not (target_at <= now_utc <= due_utc):
            continue
        event_id = _event_id(
            "due_date",
            rule.id,
            int(rule.version or 1),
            record.id,
            field_id,
            due_utc.isoformat(),
            offset_minutes,
        )
        await queue_synthetic_automation_event(
            db,
            event_id=event_id,
            workspace_id=rule.workspace_id,
            table_id=rule.table_id,
            target_automation_id=rule.id,
            event_type="due_date",
            record_id=record.id,
            after_data=data,
        )
        queued += 1
    return queued


async def scan_due_automations(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    limit: int = 100,
) -> Dict[str, int]:
    """Persist schedule/due events and advance durable cursors.

    `next_run_at` lives in the database, so process restarts do not lose the
    fact that a rule needs scanning. Synthetic event ids are deterministic so
    retrying a scan cannot duplicate an execution.
    """
    current = _aware_utc(now or utcnow())
    safe_limit = max(1, min(500, int(limit)))
    result = await db.execute(
        select(AutomationRule)
        .where(
            AutomationRule.enabled.is_(True),
            AutomationRule.deleted_at.is_(None),
            AutomationRule.next_run_at.is_not(None),
            AutomationRule.next_run_at <= current,
        )
        .order_by(AutomationRule.next_run_at.asc(), AutomationRule.id.asc())
        .limit(safe_limit)
        .with_for_update()
    )
    rules: List[AutomationRule] = list(result.scalars().all())
    scheduled = 0
    due = 0
    for rule in rules:
        trigger_type = str((rule.trigger or {}).get("type") or "")
        scheduled_for = _aware_utc(rule.next_run_at or current)
        if trigger_type == "scheduled":
            scheduled += await _queue_scheduled_rule(
                db,
                rule=rule,
                scheduled_for=scheduled_for,
            )
        elif trigger_type == "due_date":
            due += await _queue_due_rule(db, rule=rule, now=current)
        rule.next_run_at = _advance_next_run(rule, current)
        rule.updated_at = current
    if rules:
        await db.commit()
    return {"rulesScanned": len(rules), "scheduledEvents": scheduled, "dueEvents": due}
