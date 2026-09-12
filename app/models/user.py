from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Integer, Text
from sqlalchemy.orm import relationship
from app.db.base import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(Text, unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    workspace_members = relationship("WorkspaceMember", back_populates="user", cascade="all, delete-orphan")
