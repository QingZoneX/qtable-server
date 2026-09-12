from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class SkillCategory(Base):
    __tablename__ = "skill_categories"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    slug = Column(String(120), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    parent_id = Column(String, nullable=True, index=True)
    sort_order = Column(Integer, nullable=False, default=0)
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SkillRegistryEntry(Base):
    __tablename__ = "skill_registry_entries"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=True, index=True)
    category_id = Column(String, nullable=True, index=True)
    canonical_name = Column(String(255), nullable=False, index=True)
    namespace = Column(String(120), nullable=True, index=True)
    latest_version = Column(String(50), nullable=False, default="1.0.0")
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    tags = Column(JSON, nullable=True)
    visibility = Column(String(32), nullable=False, default="internal")
    source_type = Column(String(32), nullable=False, default="builtin")
    runtime_kind = Column(String(32), nullable=False, default="builtin")
    status = Column(String(32), nullable=False, default="active")
    is_enabled = Column(Boolean, nullable=False, default=True)
    entrypoint = Column(Text, nullable=True)
    module_path = Column(Text, nullable=True)
    handler_name = Column(String(120), nullable=True)
    icon = Column(String(255), nullable=True)
    manifest_json = Column(JSON, nullable=False)
    input_schema = Column(JSON, nullable=False)
    output_schema = Column(JSON, nullable=False)
    permissions_json = Column(JSON, nullable=True)
    openai_tool_schema = Column(JSON, nullable=True)
    mcp_tool_schema = Column(JSON, nullable=True)
    transport_config = Column(JSON, nullable=True)
    cache_ttl_seconds = Column(Integer, nullable=False, default=300)
    embedding_status = Column(String(32), nullable=False, default="pending")
    embedding_text = Column(Text, nullable=True)
    created_by = Column(String(64), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    skill_id = Column(String, nullable=False, index=True)
    version = Column(String(50), nullable=False, index=True)
    changelog = Column(Text, nullable=True)
    checksum = Column(String(128), nullable=True)
    is_current = Column(Boolean, nullable=False, default=False)
    manifest_json = Column(JSON, nullable=False)
    input_schema = Column(JSON, nullable=False)
    output_schema = Column(JSON, nullable=False)
    permissions_json = Column(JSON, nullable=True)
    openai_tool_schema = Column(JSON, nullable=True)
    mcp_tool_schema = Column(JSON, nullable=True)
    transport_config = Column(JSON, nullable=True)
    embedding_text = Column(Text, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SkillEmbedding(Base):
    __tablename__ = "skill_embeddings"
    __table_args__ = {"extend_existing": True}

    id = Column(String, primary_key=True)
    skill_version_id = Column(String, nullable=False, index=True)
    provider = Column(String(64), nullable=False, default="pending")
    model = Column(String(120), nullable=False, default="pending")
    vector_dim = Column(Integer, nullable=False, default=0)
    embedding = Column(JSON, nullable=True)
    content_hash = Column(String(128), nullable=True, index=True)
    indexed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
