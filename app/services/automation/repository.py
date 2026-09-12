from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import AutomationRule
from app.models.smart_table import WorkspaceItem
from app.services.automation.validation import (
    required_permission_for_actions,
    validate_automation_definition,
)
from app.services.workspace import get_effective_permission_for_item, permission_allows


class AutomationError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _schedule_from_trigger(trigger: Mapping[str, Any], *, enabled: bool) -> Optional[datetime]:
    if not enabled:
        return None
    trigger_type = str(trigger.get("type") or "")
    now = utcnow()
    if trigger_type == "scheduled":
        return now + timedelta(minutes=int(trigger.get("intervalMinutes") or 1))
    if trigger_type == "due_date":
        # Scan immediately after enable/create; subsequent scans are advanced by
        # the durable worker using scanIntervalMinutes.
        return now
    return None


async def _table_item(db: AsyncSession, table_id: str) -> WorkspaceItem:
    item = (
        await db.execute(
            select(WorkspaceItem).where(WorkspaceItem.id == str(table_id)).limit(1)
        )
    ).scalars().first()
    if item is None or str(item.type) != "table":
        raise AutomationError("Table not found")
    return item


async def _require_permission(
    db: AsyncSession,
    *,
    user_id: int,
    table_id: str,
    required: str,
) -> str:
    permission = await get_effective_permission_for_item(db, int(user_id), str(table_id))
    if not permission_allows(permission, required):
        raise PermissionError("Table not found or no access")
    return str(permission)


def serialize_automation(rule: AutomationRule) -> Dict[str, Any]:
    actions = list(rule.actions or [])
    return {
        "id": rule.id,
        "workspaceId": rule.workspace_id,
        "tableId": rule.table_id,
        "name": rule.name,
        "description": rule.description,
        "enabled": bool(rule.enabled),
        "trigger": dict(rule.trigger or {}),
        "conditions": dict(rule.conditions or {}),
        "actions": actions,
        "timezone": rule.timezone or "UTC",
        "maxRetries": int(rule.max_retries or 0),
        "version": int(rule.version or 1),
        "runAsUserId": rule.run_as_user_id,
        "requiredPermission": required_permission_for_actions(actions),
        "nextRunAt": rule.next_run_at.isoformat() if rule.next_run_at else None,
        "createdByUserId": rule.created_by_user_id,
        "updatedByUserId": rule.updated_by_user_id,
        "createdAt": rule.created_at.isoformat() if rule.created_at else None,
        "updatedAt": rule.updated_at.isoformat() if rule.updated_at else None,
    }


async def get_automation(
    db: AsyncSession,
    *,
    user_id: int,
    automation_id: str,
    required_permission: str = "read",
) -> AutomationRule:
    rule = (
        await db.execute(
            select(AutomationRule).where(
                AutomationRule.id == str(automation_id),
                AutomationRule.deleted_at.is_(None),
            ).limit(1)
        )
    ).scalars().first()
    if rule is None:
        raise AutomationError("Automation not found")
    await _require_permission(
        db,
        user_id=user_id,
        table_id=rule.table_id,
        required=required_permission,
    )
    return rule


async def list_automations(
    db: AsyncSession,
    *,
    user_id: int,
    workspace_id: Optional[str] = None,
    table_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not workspace_id and not table_id:
        raise AutomationError("workspaceId or tableId is required")
    query = select(AutomationRule).where(AutomationRule.deleted_at.is_(None))
    if workspace_id:
        query = query.where(AutomationRule.workspace_id == str(workspace_id))
    if table_id:
        query = query.where(AutomationRule.table_id == str(table_id))
    query = query.order_by(AutomationRule.updated_at.desc(), AutomationRule.id.desc())
    rules = list((await db.execute(query)).scalars().all())
    visible: List[Dict[str, Any]] = []
    for rule in rules:
        permission = await get_effective_permission_for_item(db, int(user_id), rule.table_id)
        if permission_allows(permission, "read"):
            visible.append(serialize_automation(rule))
    return visible


async def create_automation(
    db: AsyncSession,
    *,
    user_id: int,
    table_id: str,
    name: str,
    trigger: Any,
    conditions: Any,
    actions: Any,
    description: Optional[str] = None,
    timezone_name: str = "UTC",
    max_retries: int = 3,
    enabled: bool = False,
) -> AutomationRule:
    item = await _table_item(db, table_id)
    await _require_permission(db, user_id=user_id, table_id=table_id, required="edit")
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise AutomationError("Automation name is required")
    if len(normalized_name) > 191:
        raise AutomationError("Automation name is too long")
    normalized = await validate_automation_definition(
        db,
        table_id=table_id,
        trigger=trigger,
        conditions=conditions,
        actions=actions,
        timezone=timezone_name,
        max_retries=max_retries,
    )
    now = utcnow()
    rule = AutomationRule(
        id=_id("aut"),
        workspace_id=item.workspace_id,
        table_id=str(table_id),
        name=normalized_name,
        description=(str(description).strip() if description is not None else None),
        enabled=bool(enabled),
        trigger=normalized["trigger"],
        conditions=normalized["conditions"],
        actions=normalized["actions"],
        timezone=normalized["timezone"],
        max_retries=normalized["maxRetries"],
        version=1,
        run_as_user_id=int(user_id),
        next_run_at=_schedule_from_trigger(normalized["trigger"], enabled=bool(enabled)),
        created_by_user_id=int(user_id),
        updated_by_user_id=int(user_id),
        created_at=now,
        updated_at=now,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def update_automation(
    db: AsyncSession,
    *,
    user_id: int,
    automation_id: str,
    name: Optional[str] = None,
    description: Any = None,
    trigger: Any = None,
    conditions: Any = None,
    actions: Any = None,
    timezone_name: Optional[str] = None,
    max_retries: Optional[int] = None,
) -> AutomationRule:
    rule = await get_automation(
        db,
        user_id=user_id,
        automation_id=automation_id,
        required_permission="edit",
    )
    semantic_change = any(
        value is not None
        for value in (trigger, conditions, actions, timezone_name, max_retries)
    )

    if name is not None:
        normalized_name = str(name).strip()
        if not normalized_name:
            raise AutomationError("Automation name is required")
        if len(normalized_name) > 191:
            raise AutomationError("Automation name is too long")
        rule.name = normalized_name
    if description is not None:
        rule.description = str(description).strip() or None

    if semantic_change:
        normalized = await validate_automation_definition(
            db,
            table_id=rule.table_id,
            trigger=trigger if trigger is not None else rule.trigger,
            conditions=conditions if conditions is not None else rule.conditions,
            actions=actions if actions is not None else rule.actions,
            timezone=timezone_name if timezone_name is not None else rule.timezone,
            max_retries=max_retries if max_retries is not None else rule.max_retries,
        )
        rule.trigger = normalized["trigger"]
        rule.conditions = normalized["conditions"]
        rule.actions = normalized["actions"]
        rule.timezone = normalized["timezone"]
        rule.max_retries = normalized["maxRetries"]
        rule.version = int(rule.version or 1) + 1
        rule.run_as_user_id = int(user_id)
        rule.next_run_at = _schedule_from_trigger(rule.trigger, enabled=bool(rule.enabled))

    rule.updated_by_user_id = int(user_id)
    rule.updated_at = utcnow()
    await db.commit()
    await db.refresh(rule)
    return rule


async def set_automation_enabled(
    db: AsyncSession,
    *,
    user_id: int,
    automation_id: str,
    enabled: bool,
) -> AutomationRule:
    rule = await get_automation(
        db,
        user_id=user_id,
        automation_id=automation_id,
        required_permission="edit",
    )
    rule.enabled = bool(enabled)
    if rule.enabled and rule.run_as_user_id is None:
        rule.run_as_user_id = int(user_id)
    rule.next_run_at = _schedule_from_trigger(rule.trigger or {}, enabled=rule.enabled)
    rule.updated_by_user_id = int(user_id)
    rule.updated_at = utcnow()
    await db.commit()
    await db.refresh(rule)
    return rule


async def delete_automation(
    db: AsyncSession,
    *,
    user_id: int,
    automation_id: str,
) -> bool:
    rule = await get_automation(
        db,
        user_id=user_id,
        automation_id=automation_id,
        required_permission="edit",
    )
    now = utcnow()
    rule.enabled = False
    rule.next_run_at = None
    rule.deleted_at = now
    rule.updated_by_user_id = int(user_id)
    rule.updated_at = now
    await db.commit()
    return True
