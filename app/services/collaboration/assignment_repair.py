from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.change_history import ChangeItem, ChangeSet
from app.models.collaboration import UserNotification
from app.models.smart_table import WorkspaceItem
from app.models.task_profile import TableTaskProfile
from app.services.collaboration.common import workspace_members_by_id
from app.services.collaboration.notifications import (
    publish_notification_rows,
    stage_notification,
)
from app.services.workspace import get_effective_permission_for_item, permission_allows


ASSIGNMENT_REPAIR_BATCH_SIZE = 500


def _member_ids(value: Any) -> set[int]:
    result: set[int] = set()
    if isinstance(value, list):
        for item in value:
            result.update(_member_ids(item))
        return result
    if isinstance(value, Mapping):
        for key in ("id", "userId", "user_id", "value"):
            candidate = value.get(key)
            if candidate is None:
                continue
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


async def _repair_table_for_user(
    db: AsyncSession,
    *,
    table: WorkspaceItem,
    assignee_field_id: str,
    user_id: int,
) -> list[UserNotification]:
    """Replay all assignment history for one user in bounded DB pages.

    The realtime path intentionally inspects only a small recent ChangeSet
    window. This reconnect repair has a different job: guarantee eventual
    consistency after long offline periods without loading an unbounded result
    set into memory. Notification dedupe makes replay safe and idempotent.
    """
    workspace_members = await workspace_members_by_id(
        db,
        str(table.workspace_id),
        [int(user_id)],
    )
    if int(user_id) not in workspace_members:
        return []

    created: list[UserNotification] = []
    offset = 0
    while True:
        result = await db.execute(
            select(ChangeItem, ChangeSet)
            .join(ChangeSet, ChangeSet.id == ChangeItem.change_set_id)
            .where(
                ChangeItem.table_id == str(table.id),
                ChangeItem.entity_type == "record",
                ChangeSet.status == "applied",
            )
            .order_by(
                ChangeSet.created_at.asc(),
                ChangeItem.order_index.asc(),
                ChangeItem.id.asc(),
            )
            .offset(offset)
            .limit(ASSIGNMENT_REPAIR_BATCH_SIZE)
        )
        rows = list(result.all())
        if not rows:
            break

        for change_item, change_set in rows:
            changed = {str(value) for value in (change_item.changed_fields or [])}
            if assignee_field_id not in changed:
                continue
            before = (
                change_item.before_data
                if isinstance(change_item.before_data, Mapping)
                else {}
            )
            after = (
                change_item.after_data
                if isinstance(change_item.after_data, Mapping)
                else {}
            )
            newly_assigned = _member_ids(after.get(assignee_field_id)) - _member_ids(
                before.get(assignee_field_id)
            )
            if int(user_id) not in newly_assigned:
                continue
            if change_set.actor_id is not None and int(change_set.actor_id) == int(user_id):
                continue

            notification, is_new = await stage_notification(
                db,
                recipient_user_id=int(user_id),
                type="task_assigned",
                actor_id=change_set.actor_id,
                workspace_id=str(table.workspace_id),
                table_id=str(table.id),
                record_id=str(change_item.entity_id),
                comment_id=None,
                event_id=change_set.id,
                dedupe_key=(
                    f"assignment:{change_set.id}:{change_item.entity_id}:{int(user_id)}"
                ),
                payload={"source": change_set.source or "record_change"},
            )
            if is_new:
                created.append(notification)

        offset += len(rows)
        if len(rows) < ASSIGNMENT_REPAIR_BATCH_SIZE:
            break

    return created


async def repair_assignment_notifications_for_user(
    db: AsyncSession,
    *,
    user_id: int,
) -> int:
    """Durably repair missed assignments across the user's readable task tables."""
    result = await db.execute(
        select(WorkspaceItem, TableTaskProfile)
        .join(TableTaskProfile, TableTaskProfile.table_id == WorkspaceItem.id)
        .where(WorkspaceItem.type == "table")
        .order_by(WorkspaceItem.id.asc())
    )

    created: list[UserNotification] = []
    for table, profile in result.all():
        permission = await get_effective_permission_for_item(
            db,
            int(user_id),
            str(table.id),
        )
        if not permission_allows(permission, "read"):
            continue
        config = profile.config if isinstance(profile.config, Mapping) else {}
        assignee_field_id = str(config.get("assigneeFieldId") or "").strip()
        if not assignee_field_id:
            continue
        created.extend(
            await _repair_table_for_user(
                db,
                table=table,
                assignee_field_id=assignee_field_id,
                user_id=int(user_id),
            )
        )

    if created:
        await db.commit()
        await publish_notification_rows(created)
    return len(created)
