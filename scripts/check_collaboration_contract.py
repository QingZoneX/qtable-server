from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require(source: str, needle: str, label: str) -> None:
    if needle not in source:
        raise SystemExit(f"[collaboration-contract] missing {label}: {needle}")


def forbid(source: str, needle: str, label: str) -> None:
    if needle in source:
        raise SystemExit(f"[collaboration-contract] forbidden {label}: {needle}")


models = read("app/models/collaboration.py")
common = read("app/services/collaboration/common.py")
comments = read("app/services/collaboration/comments.py")
notifications = read("app/services/collaboration/notifications.py")
assignment_repair = read("app/services/collaboration/assignment_repair.py")
activity = read("app/services/collaboration/activity.py")
queries = read("app/api/graphql/queries/collaboration.py")
mutations = read("app/api/graphql/mutations/collaboration.py")
subscriptions = read("app/api/graphql/subscriptions.py")
migration = read("alembic/versions/0006_collaboration.py")
query_index = read("app/api/graphql/queries/__init__.py")
mutation_index = read("app/api/graphql/mutations/__init__.py")

for table in ("record_comments", "record_comment_mentions", "user_notifications"):
    require(models, f'__tablename__ = "{table}"', f"model table {table}")
    require(migration, f'"{table}"', f"migration table {table}")

require(migration, 'down_revision: Union[str, None] = "0005_board_card_order"', "linear migration")
require(models, "uq_user_notification_recipient_dedupe", "notification unique dedupe")
require(models, "uq_record_comment_author_client_mutation", "comment idempotency unique key")
forbid(models, "title = Column", "cached notification title")
forbid(models, "summary = Column", "cached notification summary")
forbid(models, "record_body", "cached record body")

for field in (
    "recordComments",
    "mentionCandidates",
    "notifications",
    "notificationUnreadCount",
    "recordActivity",
):
    require(queries, field, f"GraphQL query {field}")

for field in (
    "createRecordComment",
    "updateRecordComment",
    "deleteRecordComment",
    "markNotificationRead",
    "markAllNotificationsRead",
):
    require(mutations, field, f"GraphQL mutation {field}")

require(query_index, "CollaborationQueries", "query mixin registration")
require(mutation_index, "CollaborationMutations", "mutation mixin registration")
require(queries, '_require_record_permission(info, resolved, str(record_id), "read")', "comment/activity read permission")
require(mutations, '"update"', "comment write permission")
require(common, "record_is_visible", "read-time row permission")
require(notifications, "record_visibility(", "notification read-time visibility")
require(notifications, '"accessible": False', "safe tombstone")
require(notifications, "build_my_work", "due reminder Task Profile reuse")
require(notifications, "sync_assignment_notifications_for_table", "bounded assignment realtime sync")
require(notifications, "dedupe_key", "notification idempotency")
require(assignment_repair, "ASSIGNMENT_REPAIR_BATCH_SIZE", "bounded reconnect repair pages")
require(assignment_repair, ".offset(offset)", "reconnect repair pagination")
require(assignment_repair, "repair_assignment_notifications_for_user", "complete assignment reconnect repair")
require(queries, "repair_assignment_notifications_for_user", "query reconnect repair")
require(comments, 'entity_type": "comment"', "comment ChangeSet activity")
require(comments, "sanitize_markdown", "comment safety")
require(comments, "validate_workspace_members", "structured mention membership")
require(activity, "TaskTableProfile" if False else "TableTaskProfile", "activity Task Profile semantics")
forbid(activity, '"beforeData"', "raw activity before data")
forbid(activity, '"afterData"', "raw activity after data")
require(subscriptions, 'name="notificationUpdates"', "notification subscription")
require(subscriptions, "notification_broker.subscribe()", "notification broker subscription")
require(subscriptions, "table_broker.subscribe()", "assignment realtime wakeup")
require(subscriptions, "repair_assignment_notifications_for_user", "subscription reconnect repair")
require(subscriptions, "sync_due_notifications_for_user", "long-lived due sync")
require(subscriptions, "serialize_notification", "permission-safe realtime serialization")

print("[collaboration-contract] OK")
