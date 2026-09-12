from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecordComment(Base):
    """A permission-scoped discussion item attached to one table record.

    Record references intentionally are not foreign keys. Comments are soft
    deleted and may outlive a record so notification/activity APIs can return a
    safe tombstone rather than losing the audit trail or leaking stale content.
    """

    __tablename__ = "record_comments"
    __table_args__ = (
        UniqueConstraint(
            "author_id",
            "client_mutation_id",
            name="uq_record_comment_author_client_mutation",
        ),
        Index(
            "ix_record_comments_record_created",
            "table_id",
            "record_id",
            "created_at",
            "id",
        ),
    )

    id = Column(String(64), primary_key=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    table_id = Column(String(128), nullable=False, index=True)
    record_id = Column(String(128), nullable=False, index=True)
    author_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    parent_comment_id = Column(String(64), nullable=True, index=True)
    body = Column(Text, nullable=False)
    body_format = Column(String(16), nullable=False, default="markdown")
    client_mutation_id = Column(String(128), nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)


class RecordCommentMention(Base):
    """Structured mention relation; display names are never used as identity."""

    __tablename__ = "record_comment_mentions"
    __table_args__ = (
        UniqueConstraint("comment_id", "user_id", name="uq_record_comment_mention"),
        Index("ix_record_comment_mentions_user", "user_id", "comment_id"),
    )

    id = Column(String(64), primary_key=True)
    comment_id = Column(
        String(64),
        ForeignKey("record_comments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class UserNotification(Base):
    """Persisted notification envelope containing references, never stale record text.

    Title/summary/deep-link are resolved at read time after permissions are
    re-evaluated. This is the core guarantee that losing record access cannot
    leak cached record/comment content through the notification center.
    """

    __tablename__ = "user_notifications"
    __table_args__ = (
        UniqueConstraint(
            "recipient_user_id",
            "dedupe_key",
            name="uq_user_notification_recipient_dedupe",
        ),
        Index(
            "ix_user_notifications_recipient_created",
            "recipient_user_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_user_notifications_recipient_unread",
            "recipient_user_id",
            "read_at",
            "created_at",
        ),
    )

    id = Column(String(64), primary_key=True)
    recipient_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type = Column(String(32), nullable=False, index=True)
    actor_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    workspace_id = Column(String(128), nullable=True, index=True)
    table_id = Column(String(128), nullable=True, index=True)
    record_id = Column(String(128), nullable=True, index=True)
    comment_id = Column(String(64), nullable=True, index=True)
    event_id = Column(String(128), nullable=False, index=True)
    dedupe_key = Column(String(191), nullable=False)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    read_at = Column(DateTime(timezone=True), nullable=True, index=True)
