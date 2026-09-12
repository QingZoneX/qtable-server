"""QTable record collaboration service package.

The package keeps comments, notifications, activity and realtime fan-out
separate so each concern can evolve without coupling GraphQL resolvers to
persistence details.
"""

from app.services.collaboration.activity import list_record_activity
from app.services.collaboration.comments import (
    create_comment,
    delete_comment,
    list_comments,
    mention_candidates,
    update_comment,
)
from app.services.collaboration.notifications import (
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    serialize_notification,
    sync_assignment_notifications_for_table,
    sync_due_notifications_for_user,
    sync_user_notifications,
    unread_count,
)

__all__ = [
    "create_comment",
    "delete_comment",
    "list_comments",
    "mention_candidates",
    "update_comment",
    "list_record_activity",
    "list_notifications",
    "mark_all_notifications_read",
    "mark_notification_read",
    "serialize_notification",
    "sync_assignment_notifications_for_table",
    "sync_due_notifications_for_user",
    "sync_user_notifications",
    "unread_count",
]
