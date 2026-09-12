from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from graphql import GraphQLError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.graphql import schema
from app.api.graphql.queries.collaboration import CollaborationQueries
from app.core.config import settings
from app.db.base import Base
from app.models.change_history import ChangeSet
from app.models.collaboration import RecordComment, UserNotification
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
from app.services.collaboration.comments import (
    create_comment,
    delete_comment,
    list_comments,
    update_comment,
)
from app.services.collaboration.common import (
    CollaborationConflictError,
    CollaborationError,
)
from app.services.collaboration.notifications import (
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    serialize_notification,
    stage_notification,
    sync_assignment_notifications_for_table,
    sync_due_notifications_for_user,
    unread_count,
)
from app.services.smart_table_store.db_backend import update_record


ALICE = 125001
BOB = 125002
CAROL = 125003
OUTSIDER = 125004
WORKSPACE_ID = "ws-collaboration-test"
TABLE_ID = "tbl-collaboration-test"
VIEW_ID = "view-collaboration-test"
SHARED_RECORD = "record-shared"
HIDDEN_RECORD = "record-hidden"


@pytest_asyncio.fixture
async def collaboration_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'collaboration.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                User(
                    id=ALICE,
                    email="alice-collaboration@example.test",
                    name="Alice",
                    password_hash="!test",
                ),
                User(
                    id=BOB,
                    email="bob-collaboration@example.test",
                    name="Bob",
                    password_hash="!test",
                ),
                User(
                    id=CAROL,
                    email="carol-collaboration@example.test",
                    name="Carol",
                    password_hash="!test",
                ),
                User(
                    id=OUTSIDER,
                    email="outsider-collaboration@example.test",
                    name="Outsider",
                    password_hash="!test",
                ),
                Workspace(id=WORKSPACE_ID, name="Collaboration Test"),
                WorkspaceItem(
                    id="root-collaboration-test",
                    workspace_id=WORKSPACE_ID,
                    type="folder",
                    name="Root",
                    parent_id=None,
                    order_index=0,
                ),
                WorkspaceItem(
                    id=TABLE_ID,
                    workspace_id=WORKSPACE_ID,
                    type="table",
                    name="Tasks",
                    parent_id="root-collaboration-test",
                    order_index=1,
                    default_view_id=VIEW_ID,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    user_id=ALICE,
                    workspace_id=WORKSPACE_ID,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=BOB,
                    workspace_id=WORKSPACE_ID,
                    role=WorkspaceRole.editor,
                ),
                WorkspaceMember(
                    user_id=CAROL,
                    workspace_id=WORKSPACE_ID,
                    role=WorkspaceRole.editor,
                ),
                TableField(
                    id="title",
                    table_id=TABLE_ID,
                    name="任务名称",
                    type="text",
                    order_index=0,
                ),
                TableField(
                    id="status",
                    table_id=TABLE_ID,
                    name="状态",
                    type="select",
                    options=[
                        {"id": "todo", "label": "待开始"},
                        {"id": "doing", "label": "进行中"},
                        {"id": "done", "label": "已完成"},
                    ],
                    order_index=1,
                ),
                TableField(
                    id="owner",
                    table_id=TABLE_ID,
                    name="负责人",
                    type="member",
                    property={"multiple": True},
                    order_index=2,
                ),
                TableField(
                    id="due",
                    table_id=TABLE_ID,
                    name="截止日期",
                    type="date",
                    order_index=3,
                ),
                TableField(
                    id="custom",
                    table_id=TABLE_ID,
                    name="内部字段",
                    type="text",
                    order_index=4,
                ),
                TableView(
                    id=VIEW_ID,
                    table_id=TABLE_ID,
                    name="Grid",
                    type="grid",
                    config={},
                ),
                TableTaskProfile(
                    table_id=TABLE_ID,
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
                    updated_by_user_id=ALICE,
                ),
                TableRowPermissionPolicy(
                    table_id=TABLE_ID,
                    mode="member_field",
                    member_field_id="owner",
                ),
                TableRecord(
                    id=SHARED_RECORD,
                    table_id=TABLE_ID,
                    data={
                        "title": "Shared task",
                        "status": "todo",
                        "owner": [str(ALICE), str(BOB), str(CAROL)],
                        "due": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
                        "custom": "baseline",
                    },
                    order_index=0,
                    created_by_user_id=ALICE,
                    version=1,
                ),
                TableRecord(
                    id=HIDDEN_RECORD,
                    table_id=TABLE_ID,
                    data={
                        "title": "Hidden task",
                        "status": "todo",
                        "owner": [str(BOB)],
                        "due": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
                        "custom": "HIDDEN-SECRET",
                    },
                    order_index=1,
                    created_by_user_id=BOB,
                    version=1,
                ),
            ]
        )
        await session.commit()
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_comment_lifecycle_mentions_idempotency_and_history(collaboration_db):
    created = await create_comment(
        collaboration_db,
        workspace_id=WORKSPACE_ID,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        author_id=ALICE,
        body="<script>alert(1)</script> **hello** [click](javascript:alert(1))",
        mention_user_ids=[BOB],
        client_mutation_id="comment-retry-1",
    )
    assert created["revision"] == 1
    assert created["deleted"] is False
    assert "<script>" not in created["body"]
    assert "&lt;script&gt;" in created["body"]
    assert "javascript:" not in created["body"].lower()
    assert [item["id"] for item in created["mentions"]] == [BOB]

    retried = await create_comment(
        collaboration_db,
        workspace_id=WORKSPACE_ID,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        author_id=ALICE,
        body="<script>alert(1)</script> **hello** [click](javascript:alert(1))",
        mention_user_ids=[BOB],
        client_mutation_id="comment-retry-1",
    )
    assert retried["id"] == created["id"]
    comment_count = int(
        (
            await collaboration_db.execute(
                select(func.count()).select_from(RecordComment)
            )
        ).scalar()
        or 0
    )
    assert comment_count == 1

    bob_notifications = await list_notifications(
        collaboration_db,
        user_id=BOB,
        types=["comment_mention"],
    )
    assert bob_notifications["totalCount"] == 1
    assert bob_notifications["items"][0]["accessible"] is True
    assert created["body"][:20] in bob_notifications["items"][0]["summary"]

    updated = await update_comment(
        collaboration_db,
        comment_id=created["id"],
        actor_id=ALICE,
        actor_permission="edit",
        body="Edited body",
        mention_user_ids=[BOB, CAROL],
        expected_revision=1,
    )
    assert updated["revision"] == 2
    assert {item["id"] for item in updated["mentions"]} == {BOB, CAROL}

    # A recipient already mentioned on this comment is never re-notified by
    # a later edit; newly added recipients get exactly one notification.
    bob_notifications = await list_notifications(
        collaboration_db,
        user_id=BOB,
        types=["comment_mention"],
    )
    carol_notifications = await list_notifications(
        collaboration_db,
        user_id=CAROL,
        types=["comment_mention"],
    )
    assert bob_notifications["totalCount"] == 1
    assert carol_notifications["totalCount"] == 1

    with pytest.raises(CollaborationConflictError):
        await update_comment(
            collaboration_db,
            comment_id=created["id"],
            actor_id=ALICE,
            actor_permission="edit",
            body="Stale edit",
            mention_user_ids=[BOB, CAROL],
            expected_revision=1,
        )

    reply = await create_comment(
        collaboration_db,
        workspace_id=WORKSPACE_ID,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        author_id=BOB,
        body="Reply",
        parent_comment_id=created["id"],
        client_mutation_id="reply-retry-1",
    )
    assert reply["parentCommentId"] == created["id"]
    alice_replies = await list_notifications(
        collaboration_db,
        user_id=ALICE,
        types=["comment_reply"],
    )
    assert alice_replies["totalCount"] == 1

    with pytest.raises(CollaborationError, match="one level"):
        await create_comment(
            collaboration_db,
            workspace_id=WORKSPACE_ID,
            table_id=TABLE_ID,
            record_id=SHARED_RECORD,
            author_id=CAROL,
            body="Nested reply",
            parent_comment_id=reply["id"],
        )

    deleted = await delete_comment(
        collaboration_db,
        comment_id=reply["id"],
        actor_id=BOB,
        actor_permission="edit",
        expected_revision=1,
    )
    assert deleted["deleted"] is True
    assert deleted["body"] is None
    assert deleted["revision"] == 2

    page1 = await list_comments(
        collaboration_db,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        limit=1,
    )
    assert page1["totalCount"] == 2
    assert page1["pageInfo"]["hasMore"] is True
    page2 = await list_comments(
        collaboration_db,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        cursor=page1["pageInfo"]["nextCursor"],
        limit=1,
    )
    assert page2["items"][0]["id"] != page1["items"][0]["id"]

    operations = set(
        (
            await collaboration_db.execute(
                select(ChangeSet.operation).where(ChangeSet.table_id == TABLE_ID)
            )
        ).scalars().all()
    )
    assert {"comment.create", "comment.update", "comment.delete"} <= operations


@pytest.mark.asyncio
async def test_comment_rejects_non_workspace_mention_without_persisting(collaboration_db):
    before_count = int(
        (
            await collaboration_db.execute(
                select(func.count()).select_from(RecordComment)
            )
        ).scalar()
        or 0
    )
    with pytest.raises(CollaborationError, match="workspace members"):
        await create_comment(
            collaboration_db,
            workspace_id=WORKSPACE_ID,
            table_id=TABLE_ID,
            record_id=SHARED_RECORD,
            author_id=ALICE,
            body="Do not persist me",
            mention_user_ids=[OUTSIDER],
            client_mutation_id="invalid-mention",
        )
    await collaboration_db.rollback()
    after_count = int(
        (
            await collaboration_db.execute(
                select(func.count()).select_from(RecordComment)
            )
        ).scalar()
        or 0
    )
    assert after_count == before_count


@pytest.mark.asyncio
async def test_row_permission_blocks_comments_activity_and_masks_notification(collaboration_db):
    hidden_comment = await create_comment(
        collaboration_db,
        workspace_id=WORKSPACE_ID,
        table_id=TABLE_ID,
        record_id=HIDDEN_RECORD,
        author_id=BOB,
        body="Hidden discussion SECRET-COMMENT",
        mention_user_ids=[ALICE],
        client_mutation_id="hidden-comment",
    )
    assert hidden_comment["id"]

    alice = await collaboration_db.get(User, ALICE)
    info = SimpleNamespace(context={"db": collaboration_db, "user": alice})
    queries = CollaborationQueries()

    with pytest.raises(GraphQLError):
        await queries.record_comments(
            info,
            table_id=TABLE_ID,
            record_id=HIDDEN_RECORD,
        )
    with pytest.raises(GraphQLError):
        await queries.record_activity(
            info,
            table_id=TABLE_ID,
            record_id=HIDDEN_RECORD,
        )

    payload = await list_notifications(
        collaboration_db,
        user_id=ALICE,
        types=["comment_mention"],
    )
    assert payload["totalCount"] == 1
    item = payload["items"][0]
    assert item["accessible"] is False
    assert item["title"] == "内容不可访问"
    assert item["summary"] == "该内容已删除或你已无权访问。"
    assert item["deepLink"] is None
    assert item["payload"] == {}
    serialized = str(item)
    assert "Hidden task" not in serialized
    assert "SECRET-COMMENT" not in serialized
    assert "HIDDEN-SECRET" not in serialized


@pytest.mark.asyncio
async def test_notification_dedupe_read_state_and_deleted_record_tombstone(collaboration_db):
    first, first_new = await stage_notification(
        collaboration_db,
        recipient_user_id=BOB,
        type="task_assigned",
        actor_id=ALICE,
        workspace_id=WORKSPACE_ID,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        comment_id=None,
        event_id="evt-assigned-1",
        dedupe_key="dedupe-assigned-1",
        payload={"source": "test"},
    )
    duplicate, duplicate_new = await stage_notification(
        collaboration_db,
        recipient_user_id=BOB,
        type="task_assigned",
        actor_id=ALICE,
        workspace_id=WORKSPACE_ID,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        comment_id=None,
        event_id="evt-assigned-1",
        dedupe_key="dedupe-assigned-1",
        payload={"source": "test"},
    )
    await collaboration_db.commit()
    assert first_new is True
    assert duplicate_new is False
    assert duplicate.id == first.id
    assert await unread_count(collaboration_db, user_id=BOB) == 1

    marked = await mark_notification_read(
        collaboration_db,
        user_id=BOB,
        notification_id=first.id,
    )
    assert marked is not None and marked.read_at is not None
    assert await unread_count(collaboration_db, user_id=BOB) == 0

    second, _ = await stage_notification(
        collaboration_db,
        recipient_user_id=BOB,
        type="due_3d",
        actor_id=None,
        workspace_id=WORKSPACE_ID,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        comment_id=None,
        event_id="evt-due-1",
        dedupe_key="dedupe-due-1",
    )
    await collaboration_db.commit()
    assert await unread_count(collaboration_db, user_id=BOB) == 1
    assert await mark_all_notifications_read(collaboration_db, user_id=BOB) == 1
    assert await unread_count(collaboration_db, user_id=BOB) == 0

    record = (
        await collaboration_db.execute(
            select(TableRecord).where(
                TableRecord.table_id == TABLE_ID,
                TableRecord.id == SHARED_RECORD,
            )
        )
    ).scalars().one()
    await collaboration_db.delete(record)
    await collaboration_db.commit()

    tombstone = await serialize_notification(
        collaboration_db,
        notification=second,
        user_id=BOB,
    )
    assert tombstone["accessible"] is False
    assert tombstone["deepLink"] is None
    assert tombstone["payload"] == {}
    assert "Shared task" not in str(tombstone)


@pytest.mark.asyncio
async def test_assignment_and_due_notifications_use_task_profile_and_are_idempotent(collaboration_db):
    due_at = datetime.now(timezone.utc) + timedelta(hours=12)
    collaboration_db.add(
        TableRecord(
            id="record-assignment",
            table_id=TABLE_ID,
            data={
                "title": "Assignment task",
                "status": "doing",
                "owner": [str(ALICE)],
                "due": due_at.isoformat(),
                "custom": None,
            },
            order_index=10,
            created_by_user_id=ALICE,
            version=1,
        )
    )
    await collaboration_db.commit()

    await update_record(
        collaboration_db,
        TABLE_ID,
        "record-assignment",
        "owner",
        [str(BOB)],
        user_id=ALICE,
        source="graphql",
    )
    created = await sync_assignment_notifications_for_table(
        collaboration_db,
        table_id=TABLE_ID,
    )
    assert sum(1 for item in created if item.recipient_user_id == BOB) == 1
    assert await sync_assignment_notifications_for_table(
        collaboration_db,
        table_id=TABLE_ID,
    ) == []

    assignment_feed = await list_notifications(
        collaboration_db,
        user_id=BOB,
        types=["task_assigned"],
    )
    assert assignment_feed["totalCount"] == 1
    assert assignment_feed["items"][0]["title"] == "Assignment task"
    assert assignment_feed["items"][0]["deepLink"].endswith(
        "?recordId=record-assignment"
    )

    due_created = await sync_due_notifications_for_user(
        collaboration_db,
        user_id=BOB,
        timezone_name="Asia/Tokyo",
    )
    assert due_created == 1
    assert await sync_due_notifications_for_user(
        collaboration_db,
        user_id=BOB,
        timezone_name="Asia/Tokyo",
    ) == 0
    due_feed = await list_notifications(
        collaboration_db,
        user_id=BOB,
        types=["due_24h"],
    )
    assert due_feed["totalCount"] == 1
    assert due_feed["items"][0]["payload"]["timezone"] == "Asia/Tokyo"


@pytest.mark.asyncio
async def test_activity_returns_semantic_summary_without_raw_field_values(collaboration_db):
    secret = "TOP-SECRET-RAW-VALUE"
    await update_record(
        collaboration_db,
        TABLE_ID,
        SHARED_RECORD,
        "custom",
        secret,
        user_id=ALICE,
        source="graphql",
    )
    await update_record(
        collaboration_db,
        TABLE_ID,
        SHARED_RECORD,
        "status",
        "doing",
        user_id=ALICE,
        source="graphql",
    )

    activity = await list_record_activity(
        collaboration_db,
        table_id=TABLE_ID,
        record_id=SHARED_RECORD,
        limit=20,
    )
    assert activity["items"]
    text = str(activity)
    assert secret not in text
    assert "beforeData" not in text
    assert "afterData" not in text
    assert any(
        "task.status_changed" in item["kinds"]
        and item["summary"] == "更新了状态"
        for item in activity["items"]
    )
    assert any(
        item["kinds"] == ["record.updated"]
        and item["summary"] == "更新了记录"
        for item in activity["items"]
    )


def test_graphql_schema_exposes_collaboration_contract():
    rendered = str(schema.as_str())
    for field in (
        "recordComments",
        "mentionCandidates",
        "notifications",
        "notificationUnreadCount",
        "recordActivity",
        "createRecordComment",
        "updateRecordComment",
        "deleteRecordComment",
        "markNotificationRead",
        "markAllNotificationsRead",
        "notificationUpdates",
    ):
        assert field in rendered
