from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text
from app.db.base import Base

class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(Text, unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(), nullable=False)
    created_at = Column(DateTime(), nullable=False, default=datetime.utcnow)
    used_at = Column(DateTime(), nullable=True)
