from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from app.db.base import Base


class WorkspaceGenerationTrace(Base):
    """Audit trail for goal-driven workspace preview and apply."""

    __tablename__ = "workspace_generation_traces"

    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    parent_id = Column(String(128), nullable=False)
    goal = Column(Text, nullable=False)
    request_context = Column(JSON, nullable=False, default=dict)
    blueprint = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="previewed", index=True)
    created_items = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    applied_at = Column(DateTime(timezone=True), nullable=True)
