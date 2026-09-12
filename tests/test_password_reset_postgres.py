from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

import app.api.auth as auth_api
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models.password_reset import PasswordResetToken
from app.models.user import User


pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL password reset row-lock contract",
)


async def _no_rate_limit(_rules):
    return None


@pytest.mark.asyncio
async def test_forgot_and_reset_share_user_then_token_lock_order(monkeypatch):
    suffix = uuid.uuid4().hex[:12]
    email = f"password-reset-concurrency-{suffix}@example.test"
    old_raw = f"old-reset-{suffix}"
    user_id: int | None = None

    monkeypatch.setattr(auth_api, "enforce_password_reset_rate_limits", _no_rate_limit)
    monkeypatch.setattr(settings, "APP_ENV", "development")

    try:
        async with AsyncSessionLocal() as setup:
            user = User(
                email=email,
                name="Password Reset Concurrency",
                password_hash=auth_api.hash_password("old-password"),
            )
            setup.add(user)
            await setup.flush()
            user_id = user.id
            setup.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=auth_api._hash_reset_token(old_raw),
                    expires_at=datetime.utcnow() + timedelta(minutes=10),
                )
            )
            await setup.commit()

        issue_started = asyncio.Event()
        reset_started = asyncio.Event()

        async def issue_new_token():
            async with AsyncSessionLocal() as session:
                issue_started.set()
                await reset_started.wait()
                return await auth_api._create_reset_token(session, user_id, email)

        async def use_old_token():
            async with AsyncSessionLocal() as session:
                reset_started.set()
                await issue_started.wait()
                try:
                    return await auth_api.reset_password(
                        auth_api.ResetPasswordRequest(
                            token=old_raw,
                            new_password="new-password",
                        ),
                        session,
                    )
                except HTTPException as exc:
                    return exc

        issued_raw, reset_result = await asyncio.wait_for(
            asyncio.gather(issue_new_token(), use_old_token()),
            timeout=10,
        )

        assert issued_raw
        assert (
            reset_result == {"message": "Password updated"}
            or (
                isinstance(reset_result, HTTPException)
                and reset_result.status_code == 400
            )
        )

        async with AsyncSessionLocal() as verify:
            active = (
                await verify.execute(
                    select(PasswordResetToken).where(
                        PasswordResetToken.user_id == user_id,
                        PasswordResetToken.used_at.is_(None),
                    )
                )
            ).scalars().all()
            assert len(active) == 1
            assert active[0].token_hash == auth_api._hash_reset_token(issued_raw)
    finally:
        if user_id is not None:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(
                    delete(PasswordResetToken).where(
                        PasswordResetToken.user_id == user_id
                    )
                )
                await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()


@pytest.mark.asyncio
async def test_same_reset_token_can_only_commit_once(monkeypatch):
    suffix = uuid.uuid4().hex[:12]
    email = f"password-reset-once-{suffix}@example.test"
    raw = f"single-use-{suffix}"
    user_id: int | None = None

    monkeypatch.setattr(auth_api, "enforce_password_reset_rate_limits", _no_rate_limit)

    try:
        async with AsyncSessionLocal() as setup:
            user = User(
                email=email,
                name="Password Reset Single Use",
                password_hash=auth_api.hash_password("old-password"),
            )
            setup.add(user)
            await setup.flush()
            user_id = user.id
            setup.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=auth_api._hash_reset_token(raw),
                    expires_at=datetime.utcnow() + timedelta(minutes=10),
                )
            )
            await setup.commit()

        async def attempt(password: str):
            async with AsyncSessionLocal() as session:
                try:
                    return await auth_api.reset_password(
                        auth_api.ResetPasswordRequest(
                            token=raw,
                            new_password=password,
                        ),
                        session,
                    )
                except HTTPException as exc:
                    return exc

        results = await asyncio.wait_for(
            asyncio.gather(
                attempt("new-password-one"),
                attempt("new-password-two"),
            ),
            timeout=10,
        )
        success_count = sum(
            result == {"message": "Password updated"} for result in results
        )
        rejected = [
            result
            for result in results
            if isinstance(result, HTTPException)
        ]
        assert success_count == 1
        assert len(rejected) == 1
        assert rejected[0].status_code == 400
    finally:
        if user_id is not None:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(
                    delete(PasswordResetToken).where(
                        PasswordResetToken.user_id == user_id
                    )
                )
                await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.commit()
