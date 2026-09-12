from __future__ import annotations

from datetime import datetime, timedelta
import logging

import pytest
from fastapi import BackgroundTasks, HTTPException
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - register ORM relationships before test sessions
from app.api import auth as auth_api
from app.core.config import settings
from app.core.security import security_configuration_errors, verify_password
from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.services import auth_rate_limit


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(PasswordResetToken.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def safe_password_reset_defaults(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "PASSWORD_RESET_DEBUG_TOKEN_ENABLED", False)
    monkeypatch.setattr(settings, "PASSWORD_RESET_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "SMTP_HOST", None)
    monkeypatch.setattr(settings, "SMTP_FROM", None)

    async def no_rate_limit(_rules):
        return None

    monkeypatch.setattr(auth_api, "enforce_password_reset_rate_limits", no_rate_limit)


async def _create_user(db_session, email: str = "owner@example.test") -> User:
    user = User(
        email=email,
        password_hash=auth_api.hash_password("old-password"),
        name="Owner",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _forgot(db_session, email: str):
    tasks = BackgroundTasks()
    response = await auth_api.forgot_password(
        auth_api.ForgotPasswordRequest(email=email),
        tasks,
        db_session,
    )
    return response, tasks


@pytest.mark.asyncio
async def test_production_smtp_missing_is_generic_and_does_not_mint_token(db_session):
    await _create_user(db_session)

    response, tasks = await _forgot(db_session, "owner@example.test")

    assert response.model_dump(exclude_none=True) == {
        "message": auth_api._PASSWORD_RESET_GENERIC_MESSAGE,
    }
    assert tasks.tasks == []
    tokens = (await db_session.execute(select(PasswordResetToken))).scalars().all()
    assert tokens == []


@pytest.mark.asyncio
async def test_production_smtp_failure_never_becomes_response_token_or_sync_latency(
    db_session, monkeypatch
):
    await _create_user(db_session)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(settings, "SMTP_FROM", "no-reply@example.test")
    calls: list[str] = []

    def failed_delivery(_email, token):
        calls.append(token)
        return False

    monkeypatch.setattr(auth_api, "_send_reset_email", failed_delivery)

    response, tasks = await _forgot(db_session, "owner@example.test")

    assert response.model_dump(exclude_none=True) == {
        "message": auth_api._PASSWORD_RESET_GENERIC_MESSAGE,
    }
    # Remote SMTP is not contacted before the anonymous response is produced.
    assert calls == []
    assert len(tasks.tasks) == 1
    token_record = (await db_session.execute(select(PasswordResetToken))).scalars().one()
    assert len(token_record.token_hash) == 64
    assert "token" not in response.model_dump(exclude_none=True)

    # The later delivery failure still does not create an API fallback token.
    await tasks()
    assert len(calls) == 1
    assert auth_api._hash_reset_token(calls[0]) == token_record.token_hash


@pytest.mark.asyncio
async def test_known_and_unknown_accounts_have_same_external_response(db_session):
    await _create_user(db_session)

    known, _ = await _forgot(db_session, "owner@example.test")
    unknown, _ = await _forgot(db_session, "missing@example.test")

    assert known.model_dump(exclude_none=True) == unknown.model_dump(exclude_none=True)
    assert "token" not in known.model_dump(exclude_none=True)


@pytest.mark.asyncio
async def test_development_debug_token_requires_explicit_opt_in_and_db_stores_hash(
    db_session, monkeypatch
):
    user = await _create_user(db_session)
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "PASSWORD_RESET_DEBUG_TOKEN_ENABLED", True)

    response, tasks = await _forgot(db_session, user.email)

    raw = response.debug_reset_token
    assert raw
    assert tasks.tasks == []
    token = (await db_session.execute(select(PasswordResetToken))).scalars().one()
    assert token.token_hash == auth_api._hash_reset_token(raw)
    assert token.token_hash != raw


@pytest.mark.asyncio
async def test_expired_token_is_rejected_and_consumed(db_session):
    user = await _create_user(db_session)
    raw = "expired-reset-token"
    record = PasswordResetToken(
        user_id=user.id,
        token_hash=auth_api._hash_reset_token(raw),
        expires_at=datetime.utcnow() - timedelta(seconds=1),
    )
    db_session.add(record)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await auth_api.reset_password(
            auth_api.ResetPasswordRequest(token=raw, new_password="new-password"),
            db_session,
        )
    assert exc_info.value.status_code == 400
    await db_session.refresh(record)
    assert record.used_at is not None


@pytest.mark.asyncio
async def test_reset_token_is_one_time_and_success_revokes_all_siblings(db_session):
    user = await _create_user(db_session)
    primary_raw = "primary-reset-token"
    sibling_raw = "sibling-reset-token"
    expires = datetime.utcnow() + timedelta(minutes=10)
    db_session.add_all(
        [
            PasswordResetToken(
                user_id=user.id,
                token_hash=auth_api._hash_reset_token(primary_raw),
                expires_at=expires,
            ),
            PasswordResetToken(
                user_id=user.id,
                token_hash=auth_api._hash_reset_token(sibling_raw),
                expires_at=expires,
            ),
        ]
    )
    await db_session.commit()

    result = await auth_api.reset_password(
        auth_api.ResetPasswordRequest(
            token=primary_raw,
            new_password="new-password-after-reset",
        ),
        db_session,
    )
    assert result == {"message": "Password updated"}

    await db_session.refresh(user)
    assert verify_password("new-password-after-reset", user.password_hash)
    records = (
        await db_session.execute(
            select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )
    ).scalars().all()
    assert len(records) == 2
    assert all(record.used_at is not None for record in records)

    for raw in (primary_raw, sibling_raw):
        with pytest.raises(HTTPException) as exc_info:
            await auth_api.reset_password(
                auth_api.ResetPasswordRequest(token=raw, new_password="should-fail"),
                db_session,
            )
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_maps_rate_limit_to_429_with_retry_after(monkeypatch, db_session):
    async def limited(_rules):
        raise auth_rate_limit.RateLimitExceeded(42)

    monkeypatch.setattr(auth_api, "enforce_password_reset_rate_limits", limited)

    with pytest.raises(HTTPException) as exc_info:
        await _forgot(db_session, "owner@example.test")
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "42"}


@pytest.mark.asyncio
async def test_endpoint_fails_closed_when_rate_limit_backend_is_unavailable(
    monkeypatch, db_session
):
    async def unavailable(_rules):
        raise auth_rate_limit.RateLimitBackendUnavailable()

    monkeypatch.setattr(auth_api, "enforce_password_reset_rate_limits", unavailable)

    with pytest.raises(HTTPException) as exc_info:
        await _forgot(db_session, "owner@example.test")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_development_rate_limit_falls_back_locally_when_redis_is_unavailable(
    monkeypatch
):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "PASSWORD_RESET_RATE_LIMIT_ENABLED", True)
    await auth_rate_limit.reset_in_memory_rate_limits_for_tests()

    async def redis_down(_rules):
        raise RedisError("test redis outage")

    monkeypatch.setattr(auth_rate_limit, "_enforce_redis", redis_down)
    rules = [auth_rate_limit.RateLimitRule("test", "subject", 1, 60)]
    await auth_rate_limit.enforce_password_reset_rate_limits(rules)
    with pytest.raises(auth_rate_limit.RateLimitExceeded):
        await auth_rate_limit.enforce_password_reset_rate_limits(rules)


@pytest.mark.asyncio
async def test_production_rate_limit_never_falls_back_to_process_local_state(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "PASSWORD_RESET_RATE_LIMIT_ENABLED", True)

    async def redis_down(_rules):
        raise RedisError("test redis outage")

    monkeypatch.setattr(auth_rate_limit, "_enforce_redis", redis_down)
    with pytest.raises(auth_rate_limit.RateLimitBackendUnavailable):
        await auth_rate_limit.enforce_password_reset_rate_limits(
            [auth_rate_limit.RateLimitRule("test", "subject", 1, 60)]
        )


def test_rate_limit_keys_hash_email_and_token_subjects():
    email_rule = auth_rate_limit.forgot_password_rate_limit_rules(
        "Owner@Example.Test"
    )[0]
    token_rule = auth_rate_limit.reset_password_rate_limit_rules(
        auth_api._hash_reset_token("secret-reset-token")
    )[0]
    assert email_rule.subject == "owner@example.test"
    assert "owner@example.test" not in auth_rate_limit._redis_key(email_rule)
    assert "secret-reset-token" not in auth_rate_limit._redis_key(token_rule)


def test_production_startup_rejects_debug_tokens_or_disabled_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "SECRET_KEY", "a-real-production-secret-for-tests")
    monkeypatch.setattr(settings, "OAUTH_ALLOW_PLAIN_PKCE", False)
    monkeypatch.setattr(settings, "OAUTH_ALLOW_DYNAMIC_CLIENT_REGISTRATION", False)
    monkeypatch.setattr(settings, "PASSWORD_RESET_DEBUG_TOKEN_ENABLED", True)
    monkeypatch.setattr(settings, "PASSWORD_RESET_RATE_LIMIT_ENABLED", False)

    errors = security_configuration_errors()
    assert any("reset debug tokens" in error for error in errors)
    assert any("reset rate limiting" in error for error in errors)


def test_smtp_failure_log_never_contains_raw_reset_token(monkeypatch, caplog):
    raw = "never-log-this-reset-token"
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(settings, "SMTP_FROM", "no-reply@example.test")

    class BrokenSMTP:
        def __init__(self, *_args, **_kwargs):
            raise OSError("smtp unavailable")

    monkeypatch.setattr(auth_api.smtplib, "SMTP", BrokenSMTP)
    with caplog.at_level(logging.WARNING, logger=auth_api.__name__):
        assert auth_api._send_reset_email("owner@example.test", raw) is False
    assert raw not in caplog.text
