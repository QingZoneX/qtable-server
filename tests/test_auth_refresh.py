from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.api.oauth.endpoints import _pkce_method_allowed, _scope_set, _validated_scope
from app.api.oauth.pkce import (
    generate_code_challenge,
    generate_code_verifier,
    is_redirect_uri_allowed,
    is_valid_code_challenge,
    is_valid_code_verifier,
    verify_pkce,
)
from app.core.config import settings
from app.core.security import (
    JWTError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    security_configuration_errors,
)


def _legacy_token(claims: dict) -> str:
    payload = dict(claims)
    payload.setdefault("exp", datetime.now(timezone.utc) + timedelta(minutes=5))
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def test_access_and_refresh_tokens_are_not_interchangeable():
    claims = {"user_id": 1, "email": "owner@example.test"}
    access = create_access_token(claims, expires_delta=timedelta(minutes=5))
    refresh = create_refresh_token(claims, expires_delta=timedelta(days=1))

    access_claims = decode_access_token(access)
    refresh_claims = decode_refresh_token(refresh)
    assert access_claims["token_type"] == "access"
    assert refresh_claims["token_type"] == "refresh"
    assert access_claims["iss"] == settings.JWT_ISSUER
    assert access_claims["aud"] == settings.JWT_AUDIENCE
    assert access_claims["sub"] == "1"
    assert access_claims["iat"]
    assert access_claims["jti"]
    assert access_claims["scope"] == settings.JWT_DEFAULT_SCOPE
    with pytest.raises(JWTError):
        decode_access_token(refresh)
    with pytest.raises(JWTError):
        decode_refresh_token(access)


def test_strict_contract_requires_session_id_token_id_and_scope(monkeypatch):
    monkeypatch.setattr(settings, "JWT_ACCEPT_LEGACY_TOKENS", False)
    now = datetime.now(timezone.utc)
    base = {
        "user_id": 1,
        "sub": "1",
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "token_type": "access",
        "scope": settings.JWT_DEFAULT_SCOPE,
        "sid": "session-1",
        "jti": "token-1",
    }
    for missing in ("scope", "sid", "jti"):
        claims = dict(base)
        claims.pop(missing)
        token = jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        with pytest.raises(JWTError):
            decode_access_token(token)


def test_default_session_lifetimes_are_extended():
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 480
    assert settings.REFRESH_TOKEN_EXPIRE_DAYS == 30


def test_tokens_are_rotated_even_when_issued_in_the_same_second():
    claims = {"user_id": 1, "email": "owner@example.test"}
    assert create_access_token(claims) != create_access_token(claims)
    assert create_refresh_token(claims) != create_refresh_token(claims)


def test_legacy_migration_allows_missing_contract_claims_only_when_enabled(monkeypatch):
    token = _legacy_token({"user_id": 1, "email": "legacy@example.test", "token_type": "access"})
    monkeypatch.setattr(settings, "JWT_ACCEPT_LEGACY_TOKENS", True)
    assert decode_access_token(token)["user_id"] == 1
    monkeypatch.setattr(settings, "JWT_ACCEPT_LEGACY_TOKENS", False)
    with pytest.raises(JWTError):
        decode_access_token(token)


@pytest.mark.parametrize(
    "bad_claims",
    [
        {"iss": "evil"},
        {"aud": "other-product"},
        {"sub": "999"},
    ],
)
def test_legacy_compatibility_never_accepts_wrong_authority_claims(monkeypatch, bad_claims):
    monkeypatch.setattr(settings, "JWT_ACCEPT_LEGACY_TOKENS", True)
    claims = {"user_id": 1, "token_type": "access", **bad_claims}
    token = _legacy_token(claims)
    with pytest.raises(JWTError):
        decode_access_token(token)


def test_pkce_defaults_to_s256_and_enforces_rfc_shape(monkeypatch):
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    assert is_valid_code_verifier(verifier)
    assert 43 <= len(verifier) <= 128
    assert is_valid_code_challenge(challenge, "S256")
    assert verify_pkce(verifier, challenge, "S256")
    monkeypatch.setattr(settings, "OAUTH_ALLOW_PLAIN_PKCE", False)
    assert _pkce_method_allowed("S256") is True
    assert _pkce_method_allowed("plain") is False
    assert is_valid_code_verifier("short") is False
    assert is_valid_code_challenge("short", "S256") is False


def test_redirect_policy_is_exact_except_loopback_port():
    allowed = [
        "qtable://login/callback",
        "http://127.0.0.1:32123/callback?channel=desktop",
        "https://extension.example/callback",
        "https://*.chromiumapp.org/",
    ]
    assert is_redirect_uri_allowed("qtable://login/callback", allowed)
    assert is_redirect_uri_allowed(
        "http://127.0.0.1:45678/callback?channel=desktop", allowed
    )
    assert not is_redirect_uri_allowed("http://localhost:45678/callback?channel=desktop", allowed)
    assert not is_redirect_uri_allowed("http://127.0.0.1:45678/other?channel=desktop", allowed)
    assert not is_redirect_uri_allowed("https://abc.chromiumapp.org/", allowed)
    assert not is_redirect_uri_allowed("https://evil.example/callback", allowed)


def test_oauth_scope_is_limited_to_registered_client_scope():
    assert _scope_set("read write read") == {"read", "write"}
    assert _validated_scope("read", "read write") == "read"
    assert _validated_scope(None, "write read") == "read write"
    with pytest.raises(ValueError):
        _validated_scope("admin", "read write")


def test_production_rejects_unsafe_auth_configuration(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "SECRET_KEY", "development_secret_key_change_me")
    monkeypatch.setattr(settings, "OAUTH_ALLOW_PLAIN_PKCE", True)
    monkeypatch.setattr(settings, "OAUTH_ALLOW_DYNAMIC_CLIENT_REGISTRATION", True)
    errors = security_configuration_errors()
    assert any("SECRET_KEY" in error for error in errors)
    assert any("plain PKCE" in error for error in errors)
    assert any("dynamic OAuth" in error for error in errors)
