from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, JSON, String
from sqlalchemy.sql import func

from app.db.base import Base


class TableTaskProfile(Base):
    """Optional business-semantic mapping for a task/project-style table.

    The profile stores field IDs, never field names. Renaming a field therefore
    does not change business semantics. A deleted/incompatible field is kept in
    the profile and reported as invalid by the service layer so consumers never
    silently fall back to a different field.
    """

    __tablename__ = "table_task_profiles"

    table_id = Column(String(128), primary_key=True)
    config = Column(JSON, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
