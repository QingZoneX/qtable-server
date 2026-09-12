from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from app.db.base import Base


class AiVisualDesignPlan(Base):
    __tablename__ = "ai_visual_design_plans"

    id = Column(String(64), primary_key=True)
    trace_id = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    workspace_id = Column(String(128), nullable=False, index=True)
    target_type = Column(String(32), nullable=False, index=True)
    table_id = Column(String(128), nullable=True, index=True)
    parent_id = Column(String(128), nullable=True, index=True)
    dashboard_id = Column(String(128), nullable=True, index=True)
    prompt = Column(Text, nullable=False)
    request_payload = Column(JSON, nullable=False)
    proposal_payload = Column(JSON, nullable=False)
    schema_fingerprint = Column(String(64), nullable=False)
    target_fingerprint = Column(String(64), nullable=True)
    provider = Column(String(64), nullable=False, default="deterministic-fallback")
    model = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="previewed", index=True)
    result_payload = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    applied_at = Column(DateTime(timezone=True), nullable=True)
