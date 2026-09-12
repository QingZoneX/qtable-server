from sqlalchemy import Column, String, Text, DateTime
from sqlalchemy.sql import func
from app.db.base import Base


class AiMessage(Base):
    __tablename__ = "ai_messages"
    __table_args__ = {"extend_existing": True}
    
    id = Column(String, primary_key=True)
    conversation_id = Column(String, nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    table_ids = Column(Text, nullable=True)  # JSON 数组，存储此消息关联的数据表ID
    created_at = Column(DateTime(timezone=True), server_default=func.now())
