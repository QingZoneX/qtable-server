from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.db.base import Base


class MyWorkRecentTarget(Base):
    """Server-side recent navigation target for cross-device workbench recovery."""

    __tablename__ = "my_work_recent_targets"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "target_key",
            name="uq_my_work_recent_user_target",
        ),
        Index(
            "ix_my_work_recent_user_visited",
            "user_id",
            "visited_at",
        ),
    )

    id = Column(String(64), primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_key = Column(String(512), nullable=False)
    entity_type = Column(String(32), nullable=False)
    entity_id = Column(String(128), nullable=False)
    workspace_id = Column(String(128), nullable=True, index=True)
    table_id = Column(String(128), nullable=True, index=True)
    view_id = Column(String(128), nullable=True)
    visited_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
