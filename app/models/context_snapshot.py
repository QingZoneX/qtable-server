from sqlalchemy import Column, DateTime, JSON, String
from sqlalchemy.sql import func

from app.db.base import Base


class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=False, index=True)
    scope_key = Column(String(255), nullable=False, index=True)
    trace_id = Column(String, nullable=True, index=True)
    user_id = Column(String, nullable=True, index=True)
    workspace_id = Column(String, nullable=True, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    summary = Column(JSON, nullable=True)
    snapshot = Column(JSON, nullable=False)
    compression_meta = Column(JSON, nullable=True)
    source = Column(String(64), nullable=False, default="context_engine")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
