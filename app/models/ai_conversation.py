from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.sql import func
from app.db.base import Base


class AiConversation(Base):
    __tablename__ = "ai_conversations"
    __table_args__ = {"extend_existing": True}
    
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    table_id = Column(String, nullable=True)  # 保留向后兼容
    table_ids = Column(Text, nullable=True)  # JSON 数组，存储多个表 ID
    title = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
