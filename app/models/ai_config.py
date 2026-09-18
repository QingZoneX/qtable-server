from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.sql import func
from app.db.base import Base


class AiConfig(Base):
    __tablename__ = "ai_config"
    __table_args__ = {"extend_existing": True}
    
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    provider = Column(String(50), default="deepseek", nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    model = Column(String(100), default="deepseek-flash")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
