from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional
import hashlib
import logging
import secrets
import smtplib
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    JWTError,
    hash_password,
    verify_password,
)
from app.db.session import get_db
from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.services.auth_rate_limit import (
    RateLimitBackendUnavailable,
    RateLimitExceeded,
    enforce_password_reset_rate_limits,
    forgot_password_rate_limit_rules,
    reset_password_rate_limit_rules,
)
from app.services.workspace import ensure_user_default_workspace

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_PASSWORD_RESET_GENERIC_MESSAGE = (
    "If the account exists, password reset instructions will be sent."
)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


class RefreshTokenRequest(BaseModel):
    refresh_token: str


def _tokens_for(user: User, *, session_id: Optional[str] = None) -> TokenResponse:
    # sid is shared by the access/refresh pair and preserved during refresh so
    # clients can correlate one logical session while each JWT has its own jti.
    session_id = session_id or uuid.uuid4().hex
    payload = {
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "scope": settings.JWT_DEFAULT_SCOPE,
        "sid": session_id,
    }
    return TokenResponse(
        access_token=create_access_token(payload),
        refresh_token=create_refresh_token(payload),
    )


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    message: str
    # Development/test-only capability. Production rejects the corresponding
    # setting at startup and this field is excluded from normal responses.
    debug_reset_token: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _smtp_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def _debug_reset_token_enabled() -> bool:
    environment = (settings.APP_ENV or "").strip().lower()
    return bool(
        settings.PASSWORD_RESET_DEBUG_TOKEN_ENABLED
        and environment in {"dev", "development", "local", "test", "testing"}
    )


def _send_reset_email(email: str, token: str) -> bool:
    if not _smtp_configured():
        return False
    reset_link = f"{settings.RESET_PASSWORD_URL_BASE}?token={token}"
    message = EmailMessage()
    message["Subject"] = "Reset your password"
    message["From"] = settings.SMTP_FROM
    message["To"] = email
    message.set_content(
        f"Use the link below to reset your password:\n\n{reset_link}\n\n"
        f"This link expires in {settings.RESET_TOKEN_EXPIRE_MINUTES} minutes."
    )
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(message)
        return True
    except Exception as exc:
        # Never log the raw token or reset URL. Exception type is enough for
        # operators to distinguish delivery infrastructure failures.
        logger.warning(
            "Password reset email delivery failed (%s)",
            type(exc).__name__,
        )
        return False


async def _deliver_reset_email(email: str, token: str) -> None:
    # SMTP runs after the HTTP response so account existence cannot be inferred
    # from remote SMTP latency. Failure is deliberately not reflected to the
    # anonymous caller and never falls back to returning the raw credential.
    await run_in_threadpool(_send_reset_email, email, token)


async def _enforce_reset_rate_limit(rules) -> None:
    try:
        await enforce_password_reset_rate_limits(rules)
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password reset attempts. Please try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except RateLimitBackendUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password reset is temporarily unavailable.",
        ) from exc


async def _create_reset_token(db: AsyncSession, user_id: int, _email: str) -> str:
    now = datetime.utcnow()
    # Serialize token issuance for one user. Concurrent forgot-password requests
    # cannot leave multiple active reset credentials behind. All password reset
    # mutations use the same User -> PasswordResetToken lock order.
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    await db.execute(
        delete(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
    )
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_reset_token(raw_token)
    expires_at = now + timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES)
    db.add(
        PasswordResetToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    await db.commit()
    return raw_token


def _forgot_response(debug_token: Optional[str] = None) -> ForgotPasswordResponse:
    return ForgotPasswordResponse(
        message=_PASSWORD_RESET_GENERIC_MESSAGE,
        debug_reset_token=debug_token,
    )


@router.post("/register", response_model=TokenResponse)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    result = await db.execute(select(User).where(User.email == payload.email))
    existing = result.scalars().first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")
    user = User(email=payload.email, password_hash=hash_password(payload.password), name=payload.name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await ensure_user_default_workspace(db, user.id, user.name)
    return _tokens_for(user)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalars().first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    await ensure_user_default_workspace(db, user.id, user.name)
    return _tokens_for(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_session(
    payload: RefreshTokenRequest, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    try:
        claims = decode_refresh_token(payload.refresh_token)
        user_id = int(claims.get("user_id"))
    except (JWTError, TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    await ensure_user_default_workspace(db, user.id, user.name)
    return _tokens_for(user, session_id=claims.get("sid"))


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    response_model_exclude_none=True,
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> ForgotPasswordResponse:
    await _enforce_reset_rate_limit(
        forgot_password_rate_limit_rules(str(payload.email))
    )

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalars().first()
    if not user:
        return _forgot_response()

    debug_token_enabled = _debug_reset_token_enabled()
    smtp_configured = _smtp_configured()
    # Fail closed when there is no delivery path. Production does not mint an
    # otherwise-valid token that nobody can receive.
    if not smtp_configured and not debug_token_enabled:
        return _forgot_response()

    token = await _create_reset_token(db, user.id, user.email)
    if smtp_configured:
        # BackgroundTasks run only after the response has been sent. The known
        # account path therefore does not wait for SMTP while unknown accounts
        # return the exact same external response.
        background_tasks.add_task(_deliver_reset_email, user.email, token)

    return _forgot_response(token if debug_token_enabled else None)


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    token_hash = _hash_reset_token(payload.token)
    await _enforce_reset_rate_limit(
        reset_password_rate_limit_rules(token_hash)
    )

    # First resolve the owning user without taking a token lock. We then lock
    # User first and re-read the token FOR UPDATE, matching token issuance's
    # lock order and avoiding User<->Token deadlocks.
    initial = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
        )
    )
    initial_record = initial.scalars().first()
    if not initial_record:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    user_result = await db.execute(
        select(User).where(User.id == initial_record.user_id).with_for_update()
    )
    user = user_result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    token_result = await db.execute(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .with_for_update()
    )
    token_record = token_result.scalars().first()
    if not token_record:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    now = datetime.utcnow()
    if token_record.expires_at < now:
        token_record.used_at = now
        await db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    user.password_hash = hash_password(payload.new_password)
    # A successful reset revokes every outstanding reset credential for the
    # account, not only the token used for this request.
    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    await db.commit()
    return {"message": "Password updated"}
