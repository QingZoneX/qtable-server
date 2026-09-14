from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.oauth.endpoints import (
    LogoutRequest,
    RevokeTokenRequest,
    TokenRequest,
    handle_authorization_code_grant,
    handle_refresh_token_grant,
    logout_session,
    revoke_refresh_token,
    user_me,
)
from app.api.oauth.pkce import generate_code_challenge, generate_code_verifier
from app.core.config import settings
from app.core.security import decode_access_token
from app.db.base import Base
from app.models.oauth import OAuthAuthorizationCode, OAuthRefreshToken
from app.models.user import User


def _request_with_bearer(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/oauth/me",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


@pytest_asyncio.fixture
async def identity_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'identity-contract.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            User(
                id=1,
                email="identity@example.test",
                password_hash="test-only",
                name="Identity User",
            )
        )
        await session.commit()
        yield session

    await engine.dispose()


async def _issue_oauth_session(db, *, code: str = "code-1") -> dict:
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    db.add(
        OAuthAuthorizationCode(
            code=code,
            user_id=1,
            client_id="qnote",
            redirect_uri="http://127.0.0.1:14241/oauth/callback",
            scope="openid profile email qtable",
            code_challenge=challenge,
            code_challenge_method="S256",
            expires_at=datetime.utcnow() + timedelta(minutes=5),
        )
    )
    await db.commit()
    return await handle_authorization_code_grant(
        TokenRequest(
            grant_type="authorization_code",
            code=code,
            redirect_uri="http://127.0.0.1:14241/oauth/callback",
            client_id="qnote",
            code_verifier=verifier,
        ),
        db,
    )


@pytest.mark.asyncio
async def test_oauth_rotation_preserves_one_logical_session(identity_db):
    issued = await _issue_oauth_session(identity_db)
    first_claims = decode_access_token(issued["access_token"])

    assert issued["session_id"] == first_claims["sid"]
    assert first_claims["client_id"] == "qnote"
    assert first_claims["token_type"] == "access"

    first_row = (
        await identity_db.execute(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token == issued["refresh_token"]
            )
        )
    ).scalars().one()
    assert first_row.session_id == issued["session_id"]
    assert first_row.revoked_at is None

    rotated = await handle_refresh_token_grant(
        TokenRequest(
            grant_type="refresh_token",
            refresh_token=issued["refresh_token"],
            client_id="qnote",
        ),
        identity_db,
    )
    second_claims = decode_access_token(rotated["access_token"])

    assert rotated["session_id"] == issued["session_id"]
    assert second_claims["sid"] == first_claims["sid"]
    assert second_claims["jti"] != first_claims["jti"]

    await identity_db.refresh(first_row)
    assert first_row.revoked_at is not None
    second_row = (
        await identity_db.execute(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token == rotated["refresh_token"]
            )
        )
    ).scalars().one()
    assert second_row.session_id == first_row.session_id
    assert second_row.revoked_at is None


@pytest.mark.asyncio
async def test_refresh_token_revocation_is_idempotent_and_blocks_rotation(identity_db):
    issued = await _issue_oauth_session(identity_db)

    assert await revoke_refresh_token(
        RevokeTokenRequest(token=issued["refresh_token"], client_id="qnote"),
        identity_db,
    ) == {"revoked": True}
    assert await revoke_refresh_token(
        RevokeTokenRequest(token=issued["refresh_token"], client_id="qnote"),
        identity_db,
    ) == {"revoked": True}

    with pytest.raises(HTTPException) as exc_info:
        await handle_refresh_token_grant(
            TokenRequest(
                grant_type="refresh_token",
                refresh_token=issued["refresh_token"],
                client_id="qnote",
            ),
            identity_db,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_logout_can_revoke_current_session_without_touching_other_devices(identity_db):
    current = await _issue_oauth_session(identity_db, code="code-current")
    other = await _issue_oauth_session(identity_db, code="code-other")

    result = await logout_session(
        _request_with_bearer(current["access_token"]),
        LogoutRequest(all_sessions=False),
        identity_db,
    )
    assert result["all_sessions"] is False
    assert result["session_id"] == current["session_id"]
    assert result["revoked_refresh_tokens"] == 1

    rows = list((await identity_db.execute(select(OAuthRefreshToken))).scalars())
    by_session = {row.session_id: row for row in rows}
    assert by_session[current["session_id"]].revoked_at is not None
    assert by_session[other["session_id"]].revoked_at is None

    all_result = await logout_session(
        _request_with_bearer(other["access_token"]),
        LogoutRequest(all_sessions=True),
        identity_db,
    )
    assert all_result["all_sessions"] is True
    assert all_result["revoked_refresh_tokens"] == 1

    await identity_db.refresh(by_session[other["session_id"]])
    assert by_session[other["session_id"]].revoked_at is not None


@pytest.mark.asyncio
async def test_oauth_me_exposes_stable_non_secret_identity_contract(identity_db):
    issued = await _issue_oauth_session(identity_db)
    payload = await user_me(
        _request_with_bearer(issued["access_token"]),
        identity_db,
    )

    assert payload["id"] == 1
    assert payload["sub"] == "1"
    assert payload["email"] == "identity@example.test"
    assert payload["name"] == "Identity User"
    assert payload["issuer"] == "qtable"
    assert payload["audience"] == "qingzone"
    assert payload["token_type"] == "access"
    assert payload["client_id"] == "qnote"
    assert payload["session_id"] == issued["session_id"]
    assert payload["jti"]
    assert payload["issued_at"]
    assert payload["expires_at"]
    assert "openid" in payload["scopes"]

    # Profile/session metadata must not expose bearer or refresh credentials.
    assert "access_token" not in payload
    assert "refresh_token" not in payload


@pytest.mark.asyncio
async def test_oauth_me_rejects_wrong_authority(identity_db):
    # Do not use create_access_token() here: the Identity Authority correctly
    # overwrites iss/aud with its own configured values. Construct a correctly
    # signed token with a deliberately wrong issuer to test consumer rejection.
    now = datetime.now(timezone.utc)
    bad = jwt.encode(
        {
            "user_id": 1,
            "sub": "1",
            "email": "identity@example.test",
            "name": "Identity User",
            "iss": "not-qtable",
            "aud": settings.JWT_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "token_type": "access",
            "scope": settings.JWT_DEFAULT_SCOPE,
            "sid": "invalid-authority-session",
            "jti": "invalid-authority-token",
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    with pytest.raises(HTTPException) as exc_info:
        await user_me(_request_with_bearer(bad), identity_db)
    assert exc_info.value.status_code == 401
