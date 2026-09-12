from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChangeSet(Base):
    __tablename__ = "change_sets"

    id = Column(String(64), primary_key=True)
    workspace_id = Column(String(128), nullable=True, index=True)
    table_id = Column(String(128), nullable=True, index=True)
    actor_type = Column(String(32), nullable=False, default="user", index=True)
    actor_id = Column(Integer, nullable=True, index=True)
    operation = Column(String(64), nullable=False, index=True)
    source = Column(String(64), nullable=True)
    trace_id = Column(String(128), nullable=True, index=True)
    summary = Column(Text, nullable=False, default="")
    status = Column(String(32), nullable=False, default="applied", index=True)
    parent_change_set_id = Column(String(64), nullable=True, index=True)
    undone_at = Column(DateTime(timezone=True), nullable=True)
    undone_by_user_id = Column(Integer, nullable=True)
    # Keep the database default for raw/legacy writers, while normal ORM writes
    # provide a timezone-aware microsecond timestamp. SQLite CURRENT_TIMESTAMP
    # is only second-precision; that previously made a ChangeSet created after an
    # automation rule appear earlier than the rule and broke causal trigger
    # matching. The client default gives PostgreSQL and SQLite the same ordering
    # precision without a destructive schema change.
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        index=True,
    )


class ChangeItem(Base):
    __tablename__ = "change_items"

    id = Column(String(64), primary_key=True)
    change_set_id = Column(String(64), nullable=False, index=True)
    table_id = Column(String(128), nullable=False, index=True)
    entity_type = Column(String(32), nullable=False, default="record", index=True)
    entity_id = Column(String(128), nullable=False, index=True)
    before_data = Column(JSON, nullable=True)
    after_data = Column(JSON, nullable=True)
    before_meta = Column(JSON, nullable=True)
    after_meta = Column(JSON, nullable=True)
    version_before = Column(Integer, nullable=True)
    version_after = Column(Integer, nullable=True)
    changed_fields = Column(JSON, nullable=False, default=list)
    order_index = Column(Integer, nullable=False, default=0)


class RecycleBinRecord(Base):
    __tablename__ = "recycle_bin_records"

    id = Column(String(64), primary_key=True)
    table_id = Column(String(128), nullable=False, index=True)
    record_id = Column(String(128), nullable=False, index=True)
    data = Column(JSON, nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    created_by_user_id = Column(Integer, nullable=True)
    record_version = Column(Integer, nullable=False, default=1)
    deleted_by_user_id = Column(Integer, nullable=True, index=True)
    deleted_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    change_set_id = Column(String(64), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="deleted", index=True)
    restored_at = Column(DateTime(timezone=True), nullable=True)
    restored_by_user_id = Column(Integer, nullable=True)
