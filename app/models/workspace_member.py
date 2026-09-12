from __future__ import annotations

from datetime import datetime
from enum import Enum
from sqlalchemy import Column, DateTime, Enum as SAEnum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db.base import Base


class WorkspaceRole(str, Enum):
    owner = "owner"
    editor = "editor"
    viewer = "viewer"


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=True)
    # Progressive-disclosure default for this workspace. It only controls how
    # product surfaces reveal advanced controls; it never changes table/view data.
    experience_mode = Column(
        String(16),
        nullable=False,
        default="simple",
        server_default="simple",
    )
    members = relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", name="uq_workspace_member_user_workspace"),
    )

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    workspace_id = Column(String, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    role = Column(SAEnum(WorkspaceRole, name="workspace_role"), nullable=False)
    # Optional personal override. Null means "follow workspace default" so one
    # user's preference never mutates another collaborator's experience.
    experience_mode = Column(String(16), nullable=True)
    joined_at = Column(DateTime(), nullable=False, default=datetime.utcnow)
    user = relationship("User", back_populates="workspace_members")
    workspace = relationship("Workspace", back_populates="members")
