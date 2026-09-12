from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String

from app.db.base import Base


class Dashboard(Base):
    __tablename__ = "dashboards"

    id = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    is_public = Column(Boolean, nullable=False, default=False)
    public_token = Column(String, nullable=True, unique=True, index=True)
    # Public dashboard data is evaluated with the publisher's CURRENT
    # source-table permissions and row-level visibility.
    published_by_user_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class DashboardWidget(Base):
    __tablename__ = "dashboard_widgets"

    id = Column(String, primary_key=True)
    dashboard_id = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False)
    title = Column(String, nullable=True)
    color_scheme = Column(JSON, nullable=True)
    layout = Column(JSON, nullable=False)
    config = Column(JSON, nullable=True)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
