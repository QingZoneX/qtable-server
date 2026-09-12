from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
import uuid

import jwt
from jwt.exceptions import PyJWTError as JWTError
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def _identity_claims(data: Dict[str, Any], *, token_type: str, expire: datetime) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    claims = dict(data)
    user_id = claims.get("user_id")
    if user_id is not None:
        claims.setdefault("sub", str(user_id))
    claims.setdefault("scope", settings.JWT_DEFAULT_SCOPE)
    # Every token belongs to a logical session. OAuth refresh-token rotation
    # passes the existing sid explicitly; direct login/token creation gets a
    # fresh sid. jti remains unique per individual JWT.
    claims.setdefault("sid", uuid.uuid4().hex)
    claims.update({
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": now,
        "exp": expire,
        "token_type": token_type,
        "jti": uuid.uuid4().hex,
    })
    return claims


def _validate_identity_contract(payload: Dict[str, Any], *, expected_type: str) -> Dict[str, Any]:
    legacy = bool(settings.JWT_ACCEPT_LEGACY_TOKENS)
    token_type = payload.get("token_type")
    if token_type is None and legacy and expected_type == "access":
        token_type = "access"
    if token_type != expected_type:
        raise JWTError(f"Invalid {expected_type} token type")

    issuer = payload.get("iss")
    audience = payload.get("aud")
    subject = payload.get("sub")
    user_id = payload.get("user_id")
    issued_at = payload.get("iat")

    if issuer is None:
        if not legacy:
            raise JWTError("Missing token issuer")
    elif issuer != settings.JWT_ISSUER:
        raise JWTError("Invalid token issuer")

    audiences = ({str(item) for item in audience} if isinstance(audience, (list, tuple, set)) else ({str(audience)} if audience is not None else set()))
    if not audiences:
        if not legacy:
            raise JWTError("Missing token audience")
    elif settings.JWT_AUDIENCE not in audiences:
        raise JWTError("Invalid token audience")

    if subject is None:
        if not legacy:
            raise JWTError("Missing token subject")
    elif user_id is not None and str(subject) != str(user_id):
        raise JWTError("Token subject does not match user_id")

    if issued_at is None and not legacy:
        raise JWTError("Missing token issued-at claim")

    # sid identifies one logical login across refresh rotation; jti identifies
    # this individual JWT. New-contract tokens must carry both plus scope.
    if not legacy:
        if not payload.get("sid"):
            raise JWTError("Missing token session id")
        if not payload.get("jti"):
            raise JWTError("Missing token id")
        if not payload.get("scope"):
            raise JWTError("Missing token scope")
    return payload


def _decode(token: str, *, expected_type: str) -> Dict[str, Any]:
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM], options={"verify_aud": False, "verify_iss": False})
    return _validate_identity_contract(payload, expected_type=expected_type)


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    return jwt.encode(_identity_claims(data, token_type="access", expire=expire), settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    return _decode(token, expected_type="access")


def create_refresh_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))
    return jwt.encode(_identity_claims(data, token_type="refresh", expire=expire), settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_refresh_token(token: str) -> Dict[str, Any]:
    return _decode(token, expected_type="refresh")


def security_configuration_errors() -> list[str]:
    errors: list[str] = []
    if (settings.APP_ENV or "").lower() in {"prod", "production"}:
        if settings.SECRET_KEY in {"YOUR_SUPER_SECRET_KEY_CHANGE_ME", "development_secret_key_change_me", ""}:
            errors.append("Production must use a non-default SECRET_KEY")
        if settings.OAUTH_ALLOW_PLAIN_PKCE:
            errors.append("Production must not enable plain PKCE")
        if settings.OAUTH_ALLOW_DYNAMIC_CLIENT_REGISTRATION:
            errors.append("Production must not enable dynamic OAuth client registration")
        if settings.PASSWORD_RESET_DEBUG_TOKEN_ENABLED:
            errors.append("Production must not expose password reset debug tokens")
        if not settings.PASSWORD_RESET_RATE_LIMIT_ENABLED:
            errors.append("Production must keep password reset rate limiting enabled")
    return errors


__all__ = ["JWTError", "hash_password", "verify_password", "create_access_token", "create_refresh_token", "decode_access_token", "decode_refresh_token", "security_configuration_errors"]
