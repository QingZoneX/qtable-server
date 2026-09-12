from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint

from app.db.base import Base


class SourceInboxItem(Base):
    """Private per-user source inbox entry before it becomes QTable business data."""

    __tablename__ = "source_inbox_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workspace_id",
            "source_id",
            name="uq_source_inbox_user_workspace_source",
        ),
        Index("ix_source_inbox_user_workspace_status_created", "user_id", "workspace_id", "status", "created_at"),
        Index("ix_source_inbox_user_workspace_content_hash", "user_id", "workspace_id", "content_hash"),
    )

    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    source_id = Column(String(256), nullable=False)
    source_type = Column(String(32), nullable=False, default="qnote")
    url = Column(Text, nullable=True)
    canonical_url = Column(Text, nullable=True)
    page_title = Column(Text, nullable=True)
    quote = Column(Text, nullable=True)
    annotation = Column(Text, nullable=True)
    anchor = Column(JSON, nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=True)
    tags = Column(JSON, nullable=False, default=list)
    source_author = Column(String(255), nullable=True)
    screenshot_url = Column(Text, nullable=True)
    preview = Column(JSON, nullable=True)
    content_hash = Column(String(64), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="pending", index=True)
    duplicate_of_id = Column(String(64), nullable=True, index=True)
    suggestion = Column(JSON, nullable=True)
    suggestion_provider = Column(String(64), nullable=True)
    suggestion_model = Column(String(128), nullable=True)
    target_table_id = Column(String(128), nullable=True, index=True)
    target_record_id = Column(String(128), nullable=True, index=True)
    converted_change_set_id = Column(String(128), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    converted_at = Column(DateTime(timezone=True), nullable=True)
