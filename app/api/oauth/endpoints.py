"""
OAuth endpoints module
"""
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.security import create_access_token, decode_access_token, JWTError
from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.models.oauth import OAuthAuthorizationCode, OAuthClient, OAuthRefreshToken
from app.services.workspace import ensure_user_default_workspace
from .pkce import (
    generate_code_verifier,
    generate_code_challenge,
    is_redirect_uri_allowed,
    is_valid_code_challenge,
    verify_pkce,
)


router = APIRouter(prefix="/oauth", tags=["oauth"])


class AuthorizationRequest(BaseModel):
    response_type: str
    client_id: str
    redirect_uri: str
    scope: Optional[str] = None
    state: Optional[str] = None
    code_challenge: str
    code_challenge_method: str = "S256"


class TokenRequest(BaseModel):
    grant_type: str
    code: Optional[str] = None
    redirect_uri: Optional[str] = None
    client_id: Optional[str] = None
    code_verifier: Optional[str] = None
    refresh_token: Optional[str] = None


class RevokeTokenRequest(BaseModel):
    token: str
    client_id: str


class LogoutRequest(BaseModel):
    all_sessions: bool = False


def _scope_set(value: Optional[str]) -> set[str]:
    return {part for part in str(value or "").split() if part}


def _validated_scope(requested: Optional[str], allowed: Optional[str]) -> str:
    allowed_set = _scope_set(allowed)
    requested_set = _scope_set(requested)
    if requested_set and not requested_set.issubset(allowed_set):
        raise ValueError("Requested scope is not allowed for this client")
    effective = requested_set or allowed_set
    return " ".join(sorted(effective))


def _pkce_method_allowed(method: str) -> bool:
    return method == "S256" or (
        method == "plain" and settings.OAUTH_ALLOW_PLAIN_PKCE
    )


def _oauth_access_claims(
    user: User,
    scope: Optional[str],
    *,
    session_id: Optional[str] = None,
    client_id: Optional[str] = None,
) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "scope": scope or settings.JWT_DEFAULT_SCOPE,
        "sid": session_id or secrets.token_hex(16),
    }
    if client_id:
        claims["client_id"] = client_id
    return claims


async def _get_active_client(db: AsyncSession, client_id: str) -> Optional[OAuthClient]:
    result = await db.execute(
        select(OAuthClient).where(
            OAuthClient.client_id == client_id,
            OAuthClient.is_active == True,
        )
    )
    return result.scalars().first()


@router.get("/authorize")
async def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    scope: Optional[str] = None,
    state: Optional[str] = None,
    code_challenge: str = None,
    code_challenge_method: str = "S256",
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth 2.0 Authorization endpoint with PKCE
    This endpoint should be accessed by the user's browser
    """
    # Never redirect errors to an unvalidated URI. Validate the client and
    # redirect first; only then is it safe to return OAuth errors via redirect.
    client = await _get_active_client(db, client_id)
    if not client:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid client_id",
        )
    allowed_redirect_uris = [
        uri.strip() for uri in client.redirect_uris.split(",") if uri.strip()
    ]
    if not is_redirect_uri_allowed(redirect_uri, allowed_redirect_uris):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid redirect_uri",
        )

    if response_type != "code":
        error_redirect = (
            f"{redirect_uri}?error=invalid_request"
            "&error_description=Invalid+response_type"
        )
        if state:
            error_redirect += f"&state={state}"
        return RedirectResponse(url=error_redirect)

    try:
        scope = _validated_scope(scope, client.scope)
    except ValueError:
        error_redirect = (
            f"{redirect_uri}?error=invalid_scope"
            "&error_description=Requested+scope+is+not+allowed"
        )
        if state:
            error_redirect += f"&state={state}"
        return RedirectResponse(url=error_redirect)

    if (
        not code_challenge
        or not _pkce_method_allowed(code_challenge_method)
        or not is_valid_code_challenge(code_challenge, code_challenge_method)
    ):
        error_redirect = (
            f"{redirect_uri}?error=invalid_request"
            "&error_description=Valid+S256+PKCE+challenge+is+required"
        )
        if state:
            error_redirect += f"&state={state}"
        return RedirectResponse(url=error_redirect)


    # Check if user is authenticated (via session or token)
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
    
    # If no token, redirect to login page with parameters preserved
    if not token:
        from urllib.parse import urlencode
        params = {
            "response_type": response_type,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        }
        if scope:
            params["scope"] = scope
        if state:
            params["state"] = state
        
        query_string = urlencode(params)
        login_url = f"/login?{query_string}"
        return RedirectResponse(url=login_url)

    # Validate the token and get user
    try:
        payload = decode_access_token(token)
        user_id = payload.get("user_id")
        
        if not user_id:
            raise ValueError("Invalid token")
        
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalars().first()
        
        if not user:
            error_redirect = f"{redirect_uri}?error=invalid_token&error_description=User+not+found"
            if state:
                error_redirect += f"&state={state}"
            return RedirectResponse(url=error_redirect)
            
    except (JWTError, ValueError) as e:
        error_redirect = f"{redirect_uri}?error=invalid_token&error_description=Invalid+token"
        if state:
            error_redirect += f"&state={state}"
        return RedirectResponse(url=error_redirect)

    # Generate authorization code
    authorization_code = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=10)  # Code expires in 10 minutes
    
    # Store the authorization code
    auth_code_record = OAuthAuthorizationCode(
        code=authorization_code,
        user_id=user.id,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        expires_at=expires_at,
    )
    
    db.add(auth_code_record)
    await db.commit()

    # Ensure user has a default workspace
    await ensure_user_default_workspace(db, user.id, user.name)

    # Redirect back to client with authorization code
    redirect_url = f"{redirect_uri}?code={authorization_code}"
    if state:
        redirect_url += f"&state={state}"
    
    return RedirectResponse(url=redirect_url)


@router.post("/authorize-code")
async def authorize_code(
    request: Request,
    payload: AuthorizationRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth 2.0 Authorization endpoint with PKCE (JSON response)
    This endpoint returns the redirect URL as JSON instead of redirecting
    Useful for frontend applications that need to handle the redirect manually
    """
    client = await _get_active_client(db, payload.client_id)
    if not client:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid client_id",
        )

    allowed_redirect_uris = [
        uri.strip() for uri in client.redirect_uris.split(",") if uri.strip()
    ]
    if not is_redirect_uri_allowed(payload.redirect_uri, allowed_redirect_uris):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid redirect_uri",
        )

    if payload.response_type != "code":
        return {
            "redirect_url": (
                f"{payload.redirect_uri}?error=invalid_request"
                "&error_description=Invalid+response_type"
            ),
            "error": "invalid_request",
        }

    try:
        payload.scope = _validated_scope(payload.scope, client.scope)
    except ValueError:
        return {
            "redirect_url": (
                f"{payload.redirect_uri}?error=invalid_scope"
                "&error_description=Requested+scope+is+not+allowed"
            ),
            "error": "invalid_scope",
        }

    if (
        not payload.code_challenge
        or not _pkce_method_allowed(payload.code_challenge_method)
        or not is_valid_code_challenge(
            payload.code_challenge, payload.code_challenge_method
        )
    ):
        return {
            "redirect_url": (
                f"{payload.redirect_uri}?error=invalid_request"
                "&error_description=Valid+S256+PKCE+challenge+is+required"
            ),
            "error": "invalid_request",
        }


    # Check if user is authenticated (via token)
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
    
    if not token:
        return {
            "redirect_url": f"{payload.redirect_uri}?error=unauthorized&error_description=No+token+provided",
            "error": "unauthorized",
        }

    # Validate the token and get user
    try:
        token_payload = decode_access_token(token)
        user_id = token_payload.get("user_id")
        
        if not user_id:
            raise ValueError("Invalid token")
        
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalars().first()
        
        if not user:
            return {
                "redirect_url": f"{payload.redirect_uri}?error=invalid_token&error_description=User+not+found",
                "error": "invalid_token",
            }
            
    except (JWTError, ValueError) as e:
        return {
            "redirect_url": f"{payload.redirect_uri}?error=invalid_token&error_description=Invalid+token",
            "error": "invalid_token",
        }

    # Generate authorization code
    authorization_code = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=10)  # Code expires in 10 minutes
    
    # Store the authorization code
    auth_code_record = OAuthAuthorizationCode(
        code=authorization_code,
        user_id=user.id,
        client_id=payload.client_id,
        redirect_uri=payload.redirect_uri,
        scope=payload.scope,
        code_challenge=payload.code_challenge,
        code_challenge_method=payload.code_challenge_method,
        expires_at=expires_at,
    )
    
    db.add(auth_code_record)
    await db.commit()

    # Ensure user has a default workspace
    await ensure_user_default_workspace(db, user.id, user.name)

    # Build redirect URL
    redirect_url = f"{payload.redirect_uri}?code={authorization_code}"
    if payload.state:
        redirect_url += f"&state={payload.state}"
    
    return {
        "redirect_url": redirect_url,
        "code": authorization_code,
        "state": payload.state,
    }


@router.post("/token")
async def token(
    payload: TokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth 2.0 Token endpoint
    Exchanges authorization code for access token and refresh token
    Also supports refresh_token grant type to get new access tokens
    """
    # Validate grant_type
    if payload.grant_type not in ["authorization_code", "refresh_token"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid grant_type. Only 'authorization_code' and 'refresh_token' are supported.",
        )

    if payload.grant_type == "authorization_code":
        return await handle_authorization_code_grant(payload, db)
    else:
        return await handle_refresh_token_grant(payload, db)


async def handle_authorization_code_grant(payload: TokenRequest, db: AsyncSession):
    """Handle authorization_code grant type"""
    # Validate required fields
    if not payload.code or not payload.redirect_uri or not payload.client_id or not payload.code_verifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required fields for authorization_code grant",
        )

    # Find the authorization code
    result = await db.execute(
        select(OAuthAuthorizationCode)
        .where(
            OAuthAuthorizationCode.code == payload.code,
            OAuthAuthorizationCode.used_at.is_(None),
        )
        .with_for_update()
    )
    auth_code = result.scalars().first()

    if not auth_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired authorization code",
        )

    # Check if code has expired
    if auth_code.expires_at < datetime.utcnow():
        auth_code.used_at = datetime.utcnow()
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authorization code has expired",
        )

    # Validate client_id
    if auth_code.client_id != payload.client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid client_id",
        )

    # Validate redirect_uri
    if auth_code.redirect_uri != payload.redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid redirect_uri",
        )

    # Verify PKCE code verifier. S256 is mandatory by default; plain can only
    # be enabled explicitly for a controlled migration.
    if not _pkce_method_allowed(auth_code.code_challenge_method) or not verify_pkce(
        payload.code_verifier,
        auth_code.code_challenge,
        auth_code.code_challenge_method,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid code_verifier",
        )

    # Mark the authorization code as used
    auth_code.used_at = datetime.utcnow()
    await db.commit()

    # Get the user
    result = await db.execute(select(User).where(User.id == auth_code.user_id))
    user = result.scalars().first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User not found",
        )

    # Ensure user has a default workspace
    await ensure_user_default_workspace(db, user.id, user.name)

    # Create one logical OAuth session. The sid is preserved across every
    # refresh-token rotation for this login/device.
    session_id = secrets.token_hex(16)
    access_token = create_access_token(
        _oauth_access_claims(
            user,
            auth_code.scope,
            session_id=session_id,
            client_id=payload.client_id,
        )
    )

    # Create refresh token
    refresh_token_value = secrets.token_urlsafe(48)
    refresh_token_expires = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    
    refresh_token_record = OAuthRefreshToken(
        token=refresh_token_value,
        user_id=user.id,
        client_id=payload.client_id,
        scope=auth_code.scope,
        session_id=session_id,
        expires_at=refresh_token_expires,
    )
    
    db.add(refresh_token_record)
    await db.commit()

    return {
        "access_token": access_token,
        "refresh_token": refresh_token_value,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "scope": auth_code.scope or settings.JWT_DEFAULT_SCOPE,
        "session_id": session_id,
    }


async def handle_refresh_token_grant(payload: TokenRequest, db: AsyncSession):
    """Handle refresh_token grant type"""
    # Validate required fields
    if not payload.refresh_token or not payload.client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required fields for refresh_token grant",
        )

    # Find the refresh token
    result = await db.execute(
        select(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.token == payload.refresh_token,
            OAuthRefreshToken.revoked_at.is_(None),
        )
        .with_for_update()
    )
    refresh_token_record = result.scalars().first()

    if not refresh_token_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or revoked refresh token",
        )

    # Check if refresh token has expired
    if refresh_token_record.expires_at < datetime.utcnow():
        refresh_token_record.revoked_at = datetime.utcnow()
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Refresh token has expired",
        )

    # Validate client_id
    if refresh_token_record.client_id != payload.client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid client_id",
        )

    # Get the user
    result = await db.execute(select(User).where(User.id == refresh_token_record.user_id))
    user = result.scalars().first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User not found",
        )

    # Revoke the old refresh token and create its replacement in one database
    # transaction. A failed rotation must not strand a valid client session.
    refresh_token_record.revoked_at = datetime.utcnow()

    # Preserve the logical session across refresh-token rotation. Older rows
    # may predate session_id; promote them into a stable session on first refresh.
    session_id = refresh_token_record.session_id or secrets.token_hex(16)
    refresh_token_record.session_id = session_id
    access_token = create_access_token(
        _oauth_access_claims(
            user,
            refresh_token_record.scope,
            session_id=session_id,
            client_id=payload.client_id,
        )
    )

    # Create new refresh token (rotation)
    new_refresh_token_value = secrets.token_urlsafe(48)
    new_refresh_token_expires = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    
    new_refresh_token_record = OAuthRefreshToken(
        token=new_refresh_token_value,
        user_id=user.id,
        client_id=payload.client_id,
        scope=refresh_token_record.scope,
        session_id=session_id,
        expires_at=new_refresh_token_expires,
    )
    
    db.add(new_refresh_token_record)
    await db.commit()

    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token_value,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "scope": refresh_token_record.scope or settings.JWT_DEFAULT_SCOPE,
        "session_id": session_id,
    }


@router.post("/revoke")
async def revoke_refresh_token(
    payload: RevokeTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke one public-client refresh token.

    The endpoint is intentionally idempotent and does not reveal whether an
    unknown token exists. Possession of the refresh token plus the registered
    client_id is sufficient for public PKCE clients.
    """
    result = await db.execute(
        select(OAuthRefreshToken).where(
            OAuthRefreshToken.token == payload.token,
            OAuthRefreshToken.client_id == payload.client_id,
        )
    )
    record = result.scalars().first()
    if record and record.revoked_at is None:
        record.revoked_at = datetime.utcnow()
        await db.commit()
    return {"revoked": True}


@router.post("/logout")
async def logout_session(
    request: Request,
    payload: LogoutRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke the current OAuth refresh session or all sessions for the user.

    Existing access JWTs remain valid until expiry; revocation prevents any
    further refresh from the selected session(s). This is the extension point
    for a future server-side access-token denylist/JWKS-backed session service.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )
    try:
        claims = decode_access_token(auth_header.split(" ", 1)[1])
        user_id = int(claims.get("user_id"))
    except (JWTError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    session_id = claims.get("sid")
    conditions = [
        OAuthRefreshToken.user_id == user_id,
        OAuthRefreshToken.revoked_at.is_(None),
    ]
    if not payload.all_sessions:
        if not session_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current token has no session identifier",
            )
        conditions.append(OAuthRefreshToken.session_id == str(session_id))

    result = await db.execute(select(OAuthRefreshToken).where(*conditions))
    records = list(result.scalars().all())
    revoked_at = datetime.utcnow()
    for record in records:
        record.revoked_at = revoked_at
    if records:
        await db.commit()
    return {
        "revoked_refresh_tokens": len(records),
        "all_sessions": payload.all_sessions,
        "session_id": None if payload.all_sessions else str(session_id),
    }


@router.get("/userinfo")
async def userinfo(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth 2.0 UserInfo endpoint (OAuth standard)
    Returns information about the authenticated user
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )

    token = auth_header.split(" ", 1)[1]
    
    try:
        payload = decode_access_token(token)
        user_id = payload.get("user_id")
        
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )

        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalars().first()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )

        return {
            "sub": str(user.id),
            "name": user.name,
            "email": user.email,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


@router.get("/me")
async def user_me(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    User profile endpoint for browser extensions
    Returns current user information including avatar_url
    Compatible with QTable Clipper requirements
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )

    token = auth_header.split(" ", 1)[1]
    
    try:
        payload = decode_access_token(token)
        user_id = payload.get("user_id")
        
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )

        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalars().first()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )

        scopes = sorted(_scope_set(payload.get("scope")))
        return {
            # Existing Clipper-compatible fields
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "avatar_url": None,
            # Stable QingZone identity contract
            "sub": str(user.id),
            "issuer": payload.get("iss") or settings.JWT_ISSUER,
            "audience": payload.get("aud") or settings.JWT_AUDIENCE,
            "scopes": scopes,
            "token_type": payload.get("token_type") or "access",
            "session_id": payload.get("sid") or payload.get("jti"),
            "jti": payload.get("jti"),
            "client_id": payload.get("client_id"),
            "issued_at": payload.get("iat"),
            "expires_at": payload.get("exp"),
            "auth_source": "qtable",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


@router.post("/register-client")
async def register_client(
    client_name: str,
    redirect_uris: str,
    scope: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a new OAuth client (for development/testing)
    In production, this should be admin-only
    """
    if not settings.OAUTH_ALLOW_DYNAMIC_CLIENT_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dynamic OAuth client registration is disabled",
        )
    requested_redirects = [
        uri.strip() for uri in redirect_uris.split(",") if uri.strip()
    ]
    if not requested_redirects or any("*" in uri for uri in requested_redirects):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth redirect URIs must be explicit and cannot use wildcards",
        )

    import string
    import random
    
    # Generate a unique client_id
    client_id = "client_" + ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    
    client = OAuthClient(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=redirect_uris,
        scope=scope,
        is_active=True,
    )
    
    db.add(client)
    await db.commit()
    await db.refresh(client)
    
    return {
        "client_id": client.client_id,
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "scope": client.scope,
    }
