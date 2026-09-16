from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.google_sso import (
    SECURE_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    User,
    check_public_rate_limit,
    create_session_token,
    get_client_ip,
    get_current_user,
    get_optional_current_user,
    get_session_cookie_from_request,
    get_session_secret,
    get_token_exp,
    hash_token,
    is_admin_user,
    is_lan_client,
    is_plausible_google_access_token,
    is_plausible_jwt,
    verify_google_access_token,
    verify_google_token,
    verify_session_token,
)
from app.config import Settings, settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60

_LOGIN_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_WINDOW = 60.0  # seconds
_MAX_LOGIN_ATTEMPTS = 10   # per window
_MAX_TRACKED_IPS = 5000


def check_login_rate_limit(client_ip: str) -> None:
    """Enforces max login attempts per IP within sliding window to protect against credential abuse."""
    now = time.time()

    # Periodic cleanup of expired entries to prevent unbounded memory growth
    if len(_LOGIN_ATTEMPTS) > 50:
        expired_ips = [
            ip for ip, timestamps in _LOGIN_ATTEMPTS.items()
            if not timestamps or now - timestamps[-1] >= _RATE_LIMIT_WINDOW
        ]
        for ip in expired_ips:
            _LOGIN_ATTEMPTS.pop(ip, None)

    # Hard cap on dictionary size to prevent memory exhaustion under distributed spoofing
    if len(_LOGIN_ATTEMPTS) >= _MAX_TRACKED_IPS and client_ip not in _LOGIN_ATTEMPTS:
        oldest_ip = min(_LOGIN_ATTEMPTS.keys(), key=lambda k: _LOGIN_ATTEMPTS[k][-1] if _LOGIN_ATTEMPTS[k] else 0)
        _LOGIN_ATTEMPTS.pop(oldest_ip, None)

    attempts = _LOGIN_ATTEMPTS[client_ip]
    valid_attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    if len(valid_attempts) >= _MAX_LOGIN_ATTEMPTS:
        logger.warning(f"[AUDIT] [RATE_LIMITED] Too many login attempts from IP: {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again in a minute.",
            headers={"Retry-After": "60"},
        )
    valid_attempts.append(now)
    _LOGIN_ATTEMPTS[client_ip] = valid_attempts


def set_auth_session_cookie(
    response: Response, token: str, is_secure: bool, max_age: int = SEVEN_DAYS_SECONDS
) -> None:
    """
    Sets session cookie with __Host- prefix on HTTPS WAN (RFC 6265bis prefix hardening),
    falling back to standard session cookie on plain HTTP LAN.
    """
    if is_secure:
        response.set_cookie(
            key=SECURE_SESSION_COOKIE_NAME,
            value=token,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            secure=True,
            path="/",
        )
        # Clear legacy cookie if it existed
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
        )
    else:
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )


def clear_auth_session_cookie(response: Response, is_secure: bool = False) -> None:
    """Deletes both __Host- and legacy session cookies."""
    response.delete_cookie(
        key=SECURE_SESSION_COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=True,
        httponly=True,
    )
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=is_secure,
        httponly=True,
    )


class LoginRequest(BaseModel):
    credential: Optional[str] = Field(None, max_length=4096)
    access_token: Optional[str] = Field(None, max_length=2048)


class RevokeUserSessionsRequest(BaseModel):
    user_email: str = Field(..., min_length=3, max_length=254)


@router.get("/status", summary="Get Google SSO and session authentication status")
async def get_auth_status(request: Request) -> dict[str, Any]:
    """
    Public endpoint for web and mobile clients to discover auth configuration,
    Google Client ID, LAN bypass status, and whether the current session is authenticated.
    """
    cfg: Settings = getattr(request.app.state, "settings", settings)
    check_public_rate_limit(request, cfg)
    auth_enabled = bool(cfg.allowed_google_emails)
    is_lan = is_lan_client(request, cfg)
    user = await get_optional_current_user(request)
    is_authenticated = user is not None

    lan_bypass = bool(cfg.allow_lan_auth_bypass and is_lan)

    # Determine if user is authenticating specifically via Google account (vs LAN bypass fallback)
    is_lan_default_user = is_authenticated and user.email == "lan@sightline.local"
    has_google_session = is_authenticated and not is_lan_default_user and user.email != "dev@sightline.local"

    is_admin = is_admin_user(user, cfg) if is_authenticated else False

    # Mask internal LAN bypass configuration from unauthenticated WAN callers
    expose_lan_details = is_lan or is_authenticated

    # Pass session token if authenticated via session cookie so web clients can use Sec-WebSocket-Protocol
    session_token = None
    if is_authenticated and not is_lan_default_user and user.email != "dev@sightline.local":
        cookie_token = get_session_cookie_from_request(request)
        if cookie_token:
            session_token = cookie_token
        elif "authorization" in request.headers and request.headers["authorization"].lower().startswith("bearer "):
            session_token = request.headers["authorization"][7:].strip()

    return {
        "auth_enabled": auth_enabled,
        "google_client_id": cfg.google_client_id,
        "allow_lan_auth_bypass": cfg.allow_lan_auth_bypass if expose_lan_details else False,
        "lan_bypass_active": lan_bypass if expose_lan_details else False,
        "authenticated": is_authenticated,
        "has_google_session": has_google_session,
        "is_admin": is_admin,
        "is_lan": is_lan if expose_lan_details else False,
        "token": session_token,
        "user": {
            "email": user.email,
            "name": user.name,
            "picture": user.picture,
        } if is_authenticated else None,
    }


@router.post("/login", summary="Verify Google token and establish session cookie")
async def login(request: Request, response: Response, body: LoginRequest) -> dict[str, Any]:
    """
    Receives Google ID token (or OAuth access token), verifies it,
    validates the email whitelist, and creates a 7-day secure HttpOnly session cookie.
    """
    cfg: Settings = getattr(request.app.state, "settings", settings)
    client_ip = get_client_ip(request, cfg)
    check_login_rate_limit(client_ip)

    if body.credential and body.credential.strip():
        cred = body.credential.strip()
        if not is_plausible_jwt(cred):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Google ID token structure",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = await asyncio.to_thread(
            verify_google_token,
            token=cred,
            allowed_emails=cfg.allowed_google_emails,
            client_id=cfg.google_client_id,
        )
    elif body.access_token and body.access_token.strip():
        tok = body.access_token.strip()
        if not is_plausible_google_access_token(tok):
            logger.warning(f"[AUDIT] [AUTH_FAILURE] Rejected malformed Google access token from {client_ip}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Google access token format",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = await asyncio.to_thread(
            verify_google_access_token,
            access_token=tok,
            allowed_emails=cfg.allowed_google_emails,
            client_id=cfg.google_client_id,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Google credential ID token or access token",
        )

    secret = get_session_secret(cfg)
    session_token = create_session_token(user, secret, max_age_seconds=SEVEN_DAYS_SECONDS)

    # Set secure session cookie
    is_secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    set_auth_session_cookie(response, session_token, is_secure=is_secure, max_age=SEVEN_DAYS_SECONDS)

    logger.info(f"[AUDIT] [LOGIN_SUCCESS] user={user.email} ip={client_ip}")
    return {
        "status": "authenticated",
        "token": session_token,
        "user": {
            "email": user.email,
            "name": user.name,
            "picture": user.picture,
        },
    }


@router.post("/logout", summary="Sign out and revoke session token")
async def logout(request: Request, response: Response) -> dict[str, str]:
    """Clears the session cookie and blacklists verified tokens in the database to prevent reuse."""
    cfg: Settings = getattr(request.app.state, "settings", settings)
    client_ip = get_client_ip(request, cfg)
    check_login_rate_limit(client_ip)

    # Find active token from cookie or authorization header
    token = get_session_cookie_from_request(request)
    if not token and "authorization" in request.headers:
        hdr = request.headers["authorization"].strip()
        if hdr.lower().startswith("bearer "):
            token = hdr[7:].strip()

    if token and len(token) <= 4096:
        cfg: Settings = getattr(request.app.state, "settings", settings)
        secret = get_session_secret(cfg)
        # Only record token in DB if it is an authentic cryptographically signed session token
        verified_user = verify_session_token(token, secret)
        if verified_user and verified_user.email:
            db = getattr(request.app.state, "db", None)
            if db and hasattr(db, "revoke_token"):
                exp = get_token_exp(token) or (int(time.time()) + SEVEN_DAYS_SECONDS)
                token_hash = hash_token(token)
                try:
                    await db.revoke_token(token_hash, exp)
                    logger.info(f"[AUDIT] [TOKEN_REVOKED] user={verified_user.email} token_hash={token_hash[:8]}... ip={client_ip}")
                except Exception as e:
                    logger.warning(f"[auth] error recording token revocation: {e}")

    is_secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    clear_auth_session_cookie(response, is_secure=is_secure)
    return {"status": "logged_out"}


@router.get("/me", summary="Get currently authenticated user")
async def get_me(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Returns profile information for the currently authenticated user."""
    return {
        "email": current_user.email,
        "name": current_user.name,
        "picture": current_user.picture,
    }


@router.post("/revoke-other-sessions", summary="Revoke all active sessions on other devices for the current user")
async def revoke_other_sessions(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cfg: Settings = getattr(request.app.state, "settings", settings)
    client_ip = get_client_ip(request, cfg)
    check_login_rate_limit(client_ip)

    now_ts = int(time.time())
    db = getattr(request.app.state, "db", None)
    if db and hasattr(db, "revoke_user_sessions"):
        await db.revoke_user_sessions(current_user.email, now_ts)

    # Update in-memory user revocation cache
    if hasattr(request.app.state, "user_revocations") and isinstance(request.app.state.user_revocations, dict):
        request.app.state.user_revocations[current_user.email.lower()] = now_ts

    # Reissue a newly-timestamped session token (iat = now_ts + 1) for the current device
    cfg: Settings = getattr(request.app.state, "settings", settings)
    secret = get_session_secret(cfg)
    fresh_user = User(
        email=current_user.email,
        name=current_user.name,
        picture=current_user.picture,
        iat=now_ts + 1,
    )
    new_token = create_session_token(fresh_user, secret, max_age_seconds=SEVEN_DAYS_SECONDS, iat=now_ts + 1)

    is_secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    set_auth_session_cookie(response, new_token, is_secure=is_secure, max_age=SEVEN_DAYS_SECONDS)

    logger.info(f"[AUDIT] [USER_REVOKED_OTHER_SESSIONS] user={current_user.email} ip={client_ip}")
    return {
        "status": "other_sessions_revoked",
        "user_email": current_user.email,
        "token": new_token,
    }


@router.post("/revoke-user-sessions", summary="Revoke all active sessions for a specified user account (Admin only)")
async def revoke_user_sessions(
    request: Request,
    response: Response,
    body: RevokeUserSessionsRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cfg: Settings = getattr(request.app.state, "settings", settings)
    client_ip = get_client_ip(request, cfg)
    check_login_rate_limit(client_ip)
    if not is_admin_user(current_user, cfg):
        logger.warning(
            f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} attempted to revoke sessions for {body.user_email}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to revoke sessions for other accounts",
        )

    target_email = body.user_email.strip().lower()
    now_ts = int(time.time())
    db = getattr(request.app.state, "db", None)
    if db and hasattr(db, "revoke_user_sessions"):
        await db.revoke_user_sessions(target_email, now_ts)

    if hasattr(request.app.state, "user_revocations") and isinstance(request.app.state.user_revocations, dict):
        request.app.state.user_revocations[target_email] = now_ts

    logger.info(f"[AUDIT] [ADMIN_REVOKED_USER_SESSIONS] admin={current_user.email} target={target_email} ip={client_ip}")

    # If the admin revoked their own sessions, reissue a fresh token so their current session is preserved
    new_token = None
    if target_email == current_user.email.lower():
        secret = get_session_secret(cfg)
        fresh_user = User(
            email=current_user.email,
            name=current_user.name,
            picture=current_user.picture,
            iat=now_ts + 1,
        )
        new_token = create_session_token(fresh_user, secret, max_age_seconds=SEVEN_DAYS_SECONDS, iat=now_ts + 1)
        is_secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        set_auth_session_cookie(response, new_token, is_secure=is_secure, max_age=SEVEN_DAYS_SECONDS)

    return {
        "status": "user_sessions_revoked",
        "user_email": target_email,
        "token": new_token,
    }
