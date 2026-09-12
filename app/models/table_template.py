from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class TableTemplate(Base):
    """Persisted table template catalog entry.

    Templates are immutable snapshots from the perspective of table creation:
    editing a source table never changes an existing template until the template
    is explicitly updated. System templates are synchronized from repository
    definitions during startup.
    """

    __tablename__ = "table_templates"

    id = Column(String(128), primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(120), nullable=False, default="general", index=True)
    tags = Column(JSON, nullable=True)
    icon = Column(String(120), nullable=True)

    # system | personal | workspace
    scope = Column(String(32), nullable=False, index=True)
    owner_user_id = Column(Integer, nullable=True, index=True)
    workspace_id = Column(String(128), nullable=True, index=True)

    # active | archived
    status = Column(String(32), nullable=False, default="active", index=True)

    source_table_id = Column(String(128), nullable=True)
    include_records = Column(Boolean, nullable=False, default=False)
    snapshot = Column(JSON, nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)

    version = Column(Integer, nullable=False, default=1)
    usage_count = Column(Integer, nullable=False, default=0)
    featured = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
