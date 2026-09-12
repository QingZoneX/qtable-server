from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionLocal, engine
from app.models.collaboration import UserNotification
from app.models.smart_table import (
    TableField,
    TableRecord,
    TableRowPermissionPolicy,
    TableView,
    WorkspaceItem,
)
from app.models.task_profile import TableTaskProfile
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.collaboration.activity import list_record_activity
from app.services.collaboration.comments import create_comment
from app.services.collaboration.notifications import (
    list_notifications,
    serialize_notification,
    sync_assignment_notifications_for_table,
    sync_due_notifications_for_user,
)
from app.services.smart_table_store.db_backend import update_record


pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL-specific collaboration integration contract",
)


@pytest.mark.asyncio
async def test_collaboration_postgresql_comment_notification_permission_and_due_contract():
    alice = 925001
    bob = 925002
    workspace_id = "ws-collaboration-pg"
    table_id = "tbl-collaboration-pg"
    record_id = "record-collaboration-pg"
    view_id = "view-collaboration-pg"
    due_at = datetime.now(timezone.utc) + timedelta(hours=12)

    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                User(
                    id=alice,
                    email="collaboration-pg-alice@example.test",
                    name="Collaboration PG Alice",
                    password_hash="!test",
                ),
                User(
                    id=bob,
                    email="collaboration-pg-bob@example.test",
                    name="Collaboration PG Bob",
                    password_hash="!test",
                ),
                Workspace(id=workspace_id, name="Collaboration PostgreSQL"),
                WorkspaceItem(
                    id="root-collaboration-pg",
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
                    name="PG Tasks",
                    parent_id="root-collaboration-pg",
                    order_index=1,
                    default_view_id=view_id,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=alice,
                    workspace_id=workspace_id,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=bob,
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
                    type="select",
                    options=[
                        {"id": "todo", "label": "Todo"},
                        {"id": "doing", "label": "Doing"},
                        {"id": "done", "label": "Done"},
                    ],
                    order_index=1,
                ),
                TableField(
                    id="owner",
                    table_id=table_id,
                    name="Owner",
                    type="member",
                    property={"multiple": True},
                    order_index=2,
                ),
                TableField(
                    id="due",
                    table_id=table_id,
                    name="Due",
                    type="date",
                    order_index=3,
                ),
                TableField(
                    id="private",
                    table_id=table_id,
                    name="Private",
                    type="text",
                    order_index=4,
                ),
                TableView(
                    id=view_id,
                    table_id=table_id,
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableTaskProfile(
                    table_id=table_id,
                    config={
                        "schemaVersion": 1,
                        "titleFieldId": "title",
                        "statusFieldId": "status",
                        "assigneeFieldId": "owner",
                        "priorityFieldId": None,
                        "startDateFieldId": None,
                        "dueDateFieldId": "due",
                        "progressFieldId": None,
                        "parentFieldId": None,
                        "dependencyFieldId": None,
                        "workloadFieldId": None,
                        "completedStatusValues": ["done"],
                        "blockedStatusValues": [],
                        "notStartedStatusValues": ["todo"],
                    },
                    updated_by_user_id=alice,
                ),
                TableRowPermissionPolicy(
                    table_id=table_id,
                    mode="member_field",
                    member_field_id="owner",
                ),
                TableRecord(
                    id=record_id,
                    table_id=table_id,
                    data={
                        "title": "PostgreSQL task",
                        "status": "doing",
                        "owner": [str(alice), str(bob)],
                        "due": due_at.isoformat(),
                        "private": "PG-SECRET",
                    },
                    order_index=0,
                    created_by_user_id=alice,
                    version=1,
                ),
            ]
        )
        await session.commit()

        comment = await create_comment(
            session,
            workspace_id=workspace_id,
            table_id=table_id,
            record_id=record_id,
            author_id=alice,
            body="PG comment",
            mention_user_ids=[bob],
            client_mutation_id="pg-comment-idempotency",
        )
        assert comment["id"]
        mention_feed = await list_notifications(
            session,
            user_id=bob,
            types=["comment_mention"],
        )
        assert mention_feed["totalCount"] == 1
        assert mention_feed["items"][0]["title"] == "PostgreSQL task"

        # Assignment projection is driven by the explicit Task Profile field ID,
        # not a localized/guessed field name.
        await update_record(
            session,
            table_id,
            record_id,
            "owner",
            [str(alice)],
            user_id=alice,
            source="graphql",
        )
        await update_record(
            session,
            table_id,
            record_id,
            "owner",
            [str(bob)],
            user_id=alice,
            source="graphql",
        )
        created_assignments = await sync_assignment_notifications_for_table(
            session,
            table_id=table_id,
        )
        assert sum(
            1
            for notification in created_assignments
            if int(notification.recipient_user_id) == bob
        ) == 1
        assert await sync_assignment_notifications_for_table(
            session,
            table_id=table_id,
        ) == []

        due_created = await sync_due_notifications_for_user(
            session,
            user_id=bob,
            timezone_name="Asia/Tokyo",
        )
        assert due_created == 1
        assert await sync_due_notifications_for_user(
            session,
            user_id=bob,
            timezone_name="Asia/Tokyo",
        ) == 0

        activity = await list_record_activity(
            session,
            table_id=table_id,
            record_id=record_id,
            limit=20,
        )
        assert any("task.assignee_changed" in item["kinds"] for item in activity["items"])
        assert "PG-SECRET" not in str(activity)

        # Losing row permission must turn an already-persisted notification into
        # a safe tombstone on PostgreSQL too; no cached title/comment body leaks.
        row = (
            await session.execute(
                select(TableRecord).where(
                    TableRecord.table_id == table_id,
                    TableRecord.id == record_id,
                )
            )
        ).scalars().one()
        row.data = {
            **dict(row.data or {}),
            "owner": [str(alice)],
        }
        await session.commit()

        notification = (
            await session.execute(
                select(UserNotification)
                .where(
                    UserNotification.recipient_user_id == bob,
                    UserNotification.type == "comment_mention",
                )
                .limit(1)
            )
        ).scalars().one()
        tombstone = await serialize_notification(
            session,
            notification=notification,
            user_id=bob,
        )
        assert tombstone["accessible"] is False
        assert tombstone["deepLink"] is None
        assert tombstone["payload"] == {}
        assert "PostgreSQL task" not in str(tombstone)
        assert "PG comment" not in str(tombstone)
        assert "PG-SECRET" not in str(tombstone)
