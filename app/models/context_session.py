from sqlalchemy import Column, DateTime, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class ContextSession(Base):
    __tablename__ = "context_sessions"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    session_key = Column(String(255), nullable=False, unique=True, index=True)
    user_id = Column(String, nullable=True, index=True)
    workspace_id = Column(String, nullable=True, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    project_id = Column(String, nullable=True, index=True)
    table_id = Column(String, nullable=True, index=True)
    view_id = Column(String, nullable=True, index=True)
    task_id = Column(String, nullable=True, index=True)
    team_id = Column(String, nullable=True, index=True)
    organization_id = Column(String, nullable=True, index=True)
    workflow_id = Column(String, nullable=True, index=True)
    agent_id = Column(String, nullable=True, index=True)
    session_state = Column(JSON, nullable=True)
    memory_summary = Column(Text, nullable=True)
    last_context_hash = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
