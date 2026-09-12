from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from app.db.base import Base


class OAuthAuthorizationCode(Base):
    __tablename__ = "oauth_authorization_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(255), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_id = Column(String(255), nullable=False)
    redirect_uri = Column(String(512), nullable=False)
    scope = Column(String(512), nullable=True)
    code_challenge = Column(String(255), nullable=False)
    code_challenge_method = Column(String(10), nullable=False, default="S256")
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(String(255), unique=True, index=True, nullable=False)
    client_name = Column(String(255), nullable=False)
    redirect_uris = Column(String(2048), nullable=False)  # Comma-separated list of allowed redirect URIs
    scope = Column(String(512), nullable=True)  # Allowed scopes
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)


class OAuthRefreshToken(Base):
    __tablename__ = "oauth_refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(512), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_id = Column(String(255), nullable=False)
    scope = Column(String(512), nullable=True)
    # Stable OAuth session identifier. Refresh-token rotation preserves this
    # value so one device/session can be revoked without affecting others.
    session_id = Column(String(64), nullable=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship
    user = relationship("User", backref="refresh_tokens")
