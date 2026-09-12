import asyncio
from contextlib import suppress
from datetime import datetime
from typing import AsyncGenerator, Optional

import strawberry
from graphql import GraphQLError
from strawberry.scalars import JSON
from strawberry.types import Info
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graphql.helpers import (
    _resolve_backend,
    _resolve_db_table_id,
    _resolve_table_id_for_backend,
    _require_item_permission,
    _require_record_permission,
    _require_user,
    _row_permission_context,
    table_broker,
    yjs_broker,
    table_presence_broker,
    ensure_presence_cleanup_task,
    table_presence_registry,
)
from app.api.graphql.mutations.pm_agent import PMAgentSubscription
from app.models.collaboration import UserNotification
from app.services.board import board_broker
from app.services.collaboration.assignment_repair import (
    repair_assignment_notifications_for_user,
)
from app.services.collaboration.broker import notification_broker
from app.services.collaboration.common import CollaborationError
from app.services.collaboration.notifications import (
    serialize_notification,
    sync_assignment_notifications_for_table,
    sync_due_notifications_for_user,
    sync_user_notifications,
    unread_count,
)
from app.services.smart_table_store import get_full_store, resolve_table_id
from app.services.row_permissions import (
    allowed_record_ids,
    filter_store_for_user,
    row_permission_restricts_user,
)


@strawberry.type
class Subscription(PMAgentSubscription):
    """Subscription class for real-time updates."""

    @strawberry.subscription(name="tableUpdates")
    async def tableUpdates(
        self,
        info: Info,
        table_id: Optional[str] = None,
        include_snapshot: bool = True,
    ) -> AsyncGenerator[JSON, None]:
        backend = _resolve_backend(table_id)
        resolved_table_id = _resolve_table_id_for_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "read")

        async for payload in table_broker.subscribe():
            if payload.get("tableId") != resolved_table_id:
                continue
            if backend != "db":
                yield payload
                continue

            user, table_permission, _ = await _row_permission_context(
                info,
                resolved_table_id,
                "read",
            )
            db: AsyncSession = info.context["db"]
            if not include_snapshot:
                next_payload = dict(payload)
                next_payload["data"] = None
                next_payload["snapshotIncluded"] = False
                yield next_payload
                continue

            data = payload.get("data")
            if not isinstance(data, dict):
                data = await get_full_store(db, resolved_table_id)
            filtered_store, _ = await filter_store_for_user(
                db,
                resolved_table_id,
                data,
                user_id=user.id,
                table_permission=table_permission,
            )
            next_payload = dict(payload)
            next_payload["data"] = filtered_store
            next_payload["snapshotIncluded"] = True
            yield next_payload

    @strawberry.subscription(name="boardUpdates")
    async def board_updates(
        self,
        info: Info,
        table_id: str,
        view_id: str,
    ) -> AsyncGenerator[JSON, None]:
        """Emit only affected Kanban entities, re-checking row access per event."""
        if _resolve_backend(table_id) != "db":
            raise GraphQLError("Kanban realtime requires database backend")
        resolved_table_id = _resolve_db_table_id(table_id)
        await _require_item_permission(info, resolved_table_id, "read")

        async for payload in board_broker.subscribe():
            if payload.get("tableId") != resolved_table_id:
                continue
            if payload.get("viewId") != view_id:
                continue
            record_id = payload.get("recordId")
            if record_id:
                try:
                    await _require_record_permission(
                        info,
                        resolved_table_id,
                        str(record_id),
                        "read",
                    )
                except GraphQLError:
                    continue
            yield dict(payload)

    @strawberry.subscription(name="notificationUpdates")
    async def notification_updates(
        self,
        info: Info,
        timezone: str = "UTC",
    ) -> AsyncGenerator[JSON, None]:
        """Durable notification stream with reconnect repair and due polling.

        Persisted rows are the source of truth. Broker events only wake the
        subscriber. Every yielded notification is serialized after current
        table/row permission is re-evaluated, so a lost permission becomes a
        safe tombstone instead of leaking cached titles or comment text.
        """
        user = await _require_user(info)
        db: AsyncSession = info.context["db"]
        try:
            # Reconnect repair is intentionally complete and user-scoped. The
            # realtime table-event path below stays bounded for low latency.
            await repair_assignment_notifications_for_user(
                db,
                user_id=user.id,
            )
            await sync_user_notifications(
                db,
                user_id=user.id,
                timezone_name=timezone,
            )
        except CollaborationError as exc:
            raise GraphQLError(str(exc)) from exc
        yield {
            "kind": "snapshot",
            "notification": None,
            "unreadCount": await unread_count(db, user_id=user.id),
            "updatedAt": datetime.utcnow().isoformat(),
        }

        notification_stream = notification_broker.subscribe().__aiter__()
        table_stream = table_broker.subscribe().__aiter__()
        notification_task = asyncio.create_task(notification_stream.__anext__())
        table_task = asyncio.create_task(table_stream.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {notification_task, table_task},
                    timeout=900,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # The 15-minute tick closes the gap for date thresholds
                    # while a browser tab remains open. QTable#146 will own
                    # user-configurable schedules later.
                    await sync_due_notifications_for_user(
                        db,
                        user_id=user.id,
                        timezone_name=timezone,
                    )
                    continue

                if notification_task in done:
                    event = notification_task.result()
                    notification_task = asyncio.create_task(
                        notification_stream.__anext__()
                    )
                    if int(event.get("recipientUserId") or 0) == int(user.id):
                        notification = None
                        notification_id = event.get("notificationId")
                        if notification_id:
                            row = (
                                await db.execute(
                                    select(UserNotification).where(
                                        UserNotification.id == str(notification_id),
                                        UserNotification.recipient_user_id == user.id,
                                    ).limit(1)
                                )
                            ).scalars().first()
                            if row is not None:
                                notification = await serialize_notification(
                                    db,
                                    notification=row,
                                    user_id=user.id,
                                )
                        yield {
                            "kind": event.get("kind") or "changed",
                            "notification": notification,
                            "unreadCount": await unread_count(db, user_id=user.id),
                            "updatedAt": event.get("updatedAt") or datetime.utcnow().isoformat(),
                        }

                if table_task in done:
                    event = table_task.result()
                    table_task = asyncio.create_task(table_stream.__anext__())
                    changed_table_id = event.get("tableId")
                    if changed_table_id and _resolve_backend(str(changed_table_id)) == "db":
                        try:
                            await sync_assignment_notifications_for_table(
                                db,
                                table_id=_resolve_db_table_id(str(changed_table_id)),
                            )
                        except CollaborationError:
                            # The reconnect/query repair path retries safely; a
                            # table invalidation must not terminate the stream.
                            pass
        finally:
            for task in (notification_task, table_task):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            with suppress(Exception):
                await notification_stream.aclose()
            with suppress(Exception):
                await table_stream.aclose()

    @strawberry.subscription(name="yjsUpdates")
    async def yjsUpdates(
        self,
        info: Info,
        doc_id: str,
    ) -> AsyncGenerator[JSON, None]:
        table_id = resolve_table_id(doc_id) or doc_id
        backend = _resolve_backend(table_id)
        resolved_table_id = (
            _resolve_db_table_id(table_id) if backend == "db" else table_id
        )
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "read")

        async for payload in yjs_broker.subscribe():
            if payload.get("docId") != doc_id:
                continue
            if backend != "db":
                yield payload
                continue

            user, table_permission, row_policy = await _row_permission_context(
                info,
                resolved_table_id,
                "read",
            )
            if not row_permission_restricts_user(row_policy, table_permission):
                yield payload
                continue

            db: AsyncSession = info.context["db"]
            allowed = await allowed_record_ids(
                db,
                resolved_table_id,
                user_id=user.id,
                table_permission=table_permission,
                policy=row_policy,
            )
            incoming_patches = payload.get("recordPatches")
            filtered_patches = []
            if isinstance(incoming_patches, list):
                filtered_patches = [
                    patch
                    for patch in incoming_patches
                    if isinstance(patch, dict)
                    and str(patch.get("recordId") or "") in allowed
                ]

            if not filtered_patches:
                continue
            next_payload = {
                "docId": doc_id,
                "update": "",
                "clientId": payload.get("clientId"),
                "recordPatches": filtered_patches,
            }
            yield next_payload

    @strawberry.subscription(name="tablePresenceUpdates")
    async def tablePresenceUpdates(
        self,
        info: Info,
        table_id: str,
    ) -> AsyncGenerator[JSON, None]:
        backend = _resolve_backend(table_id)
        resolved_table_id = _resolve_table_id_for_backend(table_id)
        if backend == "db":
            await _require_item_permission(info, resolved_table_id, "read")
        await ensure_presence_cleanup_task()
        viewers = await table_presence_registry.get_viewers(resolved_table_id)
        yield {
            "tableId": resolved_table_id,
            "updatedAt": datetime.utcnow().isoformat(),
            "viewers": viewers,
        }
        async for payload in table_presence_broker.subscribe():
            if payload.get("tableId") == resolved_table_id:
                yield payload
