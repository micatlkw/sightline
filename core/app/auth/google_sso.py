from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional
import urllib.error
import urllib.request

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.requests import HTTPConnection

from app.config import Settings, settings

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "sightline_session"
SECURE_SESSION_COOKIE_NAME = "__Host-sightline_session"


def get_session_cookie_from_request(conn: HTTPConnection | None) -> str | None:
    """Extracts session token from cookies, prioritizing the secure __Host- prefix."""
    if not conn or not hasattr(conn, "cookies") or not conn.cookies:
        return None
    return conn.cookies.get(SECURE_SESSION_COOKIE_NAME) or conn.cookies.get(SESSION_COOKIE_NAME)


# HTTPBearer optional security scheme
security = HTTPBearer(auto_error=False)
_google_request = google_requests.Request()


@dataclass
class User:
    email: str
    name: Optional[str] = None
    picture: Optional[str] = None
    iat: Optional[int] = None


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64decode(s: str) -> bytes:
    padding = 4 - (len(s) % 4)
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


_EPHEMERAL_SESSION_SECRET: Optional[str] = None


def get_session_secret(cfg: Settings) -> str:
    """
    Retrieve or generate a persistent HMAC session signing secret key.
    Persisted to .session_secret in the DB directory.
    """
    global _EPHEMERAL_SESSION_SECRET
    if getattr(cfg, "session_secret_key", None):
        return cfg.session_secret_key

    secret_file = cfg.db_path.parent / ".session_secret"
    try:
        if secret_file.exists():
            try:
                secret_file.chmod(0o600)
            except OSError:
                pass
            secret = secret_file.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        new_secret = secrets.token_hex(32)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(new_secret, encoding="utf-8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
        return new_secret
    except Exception as exc:
        logger.warning(f"[auth] could not persist session secret to {secret_file}: {exc}")
        if not _EPHEMERAL_SESSION_SECRET:
            _EPHEMERAL_SESSION_SECRET = secrets.token_hex(32)
        return _EPHEMERAL_SESSION_SECRET


def create_session_token(
    user: User,
    secret: str,
    max_age_seconds: int = 7 * 86400,
    iat: Optional[int] = None,
) -> str:
    """Generates a secure, signed HMAC session token containing user claims and issue timestamp."""
    now_ts = int(time.time())
    token_iat = iat if iat is not None else (user.iat if user and user.iat is not None else now_ts)
    payload = {
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "exp": now_ts + max_age_seconds,
        "iat": token_iat,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64encode(payload_bytes)
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64encode(sig)
    return f"{payload_b64}.{sig_b64}"


def verify_session_token(token: str, secret: str) -> Optional[User]:
    """Validates signature, expiration, and claims of an HMAC session token."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts
        expected_sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        actual_sig = _b64decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
        if payload.get("exp", 0) < time.time():
            return None
        return User(
            email=payload.get("email", ""),
            name=payload.get("name"),
            picture=payload.get("picture"),
            iat=payload.get("iat"),
        )
    except Exception:
        return None


def hash_token(token: str) -> str:
    """Computes a SHA-256 hash of a session token for revocation tracking."""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def get_token_exp(token: str) -> Optional[int]:
    """Extracts expiration timestamp from an HMAC session token payload."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload = json.loads(_b64decode(parts[0]).decode("utf-8"))
        return int(payload.get("exp", 0))
    except Exception:
        return None


def is_plausible_jwt(token: str) -> bool:
    """Quick structural check to verify a string is a 3-part JWT before verifying."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        return False
    try:
        header_json = _b64decode(parts[0]).decode("utf-8")
        header = json.loads(header_json)
        return isinstance(header, dict) and "alg" in header
    except Exception:
        return False


_GOOGLE_ACCESS_TOKEN_REGEX = re.compile(r"^[A-Za-z0-9._~-]+$")


def is_plausible_google_access_token(token: str) -> bool:
    """
    Validates structural plausibility of a Google OAuth2 access token
    (length and standard RFC 6750 / Google token charset) before initiating outbound HTTP requests.
    Prevents thread exhaustion DoS from malformed or hostile token strings.
    """
    s = token.strip()
    if not (8 <= len(s) <= 1024):
        return False
    return bool(_GOOGLE_ACCESS_TOKEN_REGEX.match(s))


def is_user_session_revoked(user: Optional[User], user_revocations: Optional[dict[str, int]]) -> bool:
    """Checks if a user's session token was issued prior to their latest session revocation timestamp."""
    if not user or not user.email or not user_revocations:
        return False
    email_key = user.email.lower().strip()
    if email_key in user_revocations:
        revoked_before = user_revocations[email_key]
        if user.iat is None or user.iat < revoked_before:
            return True
    return False



LAN_PRIVATE_NETWORKS = [
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918 Private & Docker host/bridge gateway subnets
    ipaddress.ip_network("fe80::/10"),  # IPv6 Link-Local
    ipaddress.ip_network("fc00::/7"),   # IPv6 Unique Local Address
]

TRUSTED_PROXIES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),  # Docker container bridge networks
    ipaddress.ip_network("::1/128"),
]

LAN_ORIGIN_REGEX = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?$"
)


def is_allowed_ws_origin(origin: Optional[str], cfg: Optional[Settings] = None) -> bool:
    """
    Validates WebSocket Origin header against authorized origins to prevent CSWSH.
    Returns True if:
    1. Origin is missing/empty (non-browser clients, native mobile apps, CLI/scripts)
    2. Origin matches configured public domain (e.g. https://sightline.yourdomain.com:4210)
    3. Origin matches capacitor / localhost webviews
    4. Origin matches private LAN subnet origins (192.168.x.x, 10.x.x.x, 127.0.0.1)
    """
    if not origin or not origin.strip():
        return True

    clean_origin = origin.strip().lower().rstrip("/")

    if clean_origin in ("capacitor://localhost", "http://localhost", "https://localhost"):
        return True

    effective_cfg = cfg or settings
    if effective_cfg and effective_cfg.domain_name and effective_cfg.domain_name.strip():
        dom = effective_cfg.domain_name.strip().lower()
        if ":" in dom:
            dom = dom.split(":")[0]

        allowed_domains = [
            f"https://{dom}",
        ]
        if effective_cfg.https_port:
            allowed_domains.append(f"https://{dom}:{effective_cfg.https_port}")

        for allowed in allowed_domains:
            if clean_origin == allowed.lower().rstrip("/"):
                return True

    if LAN_ORIGIN_REGEX.match(clean_origin):
        return True

    return False


def get_client_ip(connection: HTTPConnection, cfg: Optional[Settings] = None) -> str:
    """
    Extracts the verified client IP address.
    If the direct TCP peer is a trusted reverse proxy (Caddy / Docker bridge network / loopback):
      - Prioritizes CF-Connecting-IP (injected by Cloudflare edge / tunnel)
      - Next checks X-Real-IP (injected by Caddy)
      - Next checks X-Forwarded-For (first valid IP entry in list)
      - Falls back to peer IP
    If the direct TCP peer is NOT a trusted reverse proxy (e.g. direct TCP connection to port 8000):
      - Disregards all client-supplied proxy headers to prevent IP spoofing,
        and strictly returns connection.client.host.
    """
    peer_host = connection.client.host.strip() if connection.client and connection.client.host else ""
    if not peer_host:
        return "unknown"

    is_trusted_peer = False
    if peer_host in ("testclient", "localhost", "127.0.0.1", "::1"):
        is_trusted_peer = True
    else:
        try:
            peer_ip_obj = ipaddress.ip_address(peer_host.strip("[]"))
            for proxy_net in TRUSTED_PROXIES:
                if peer_ip_obj in proxy_net:
                    is_trusted_peer = True
                    break
        except ValueError:
            is_trusted_peer = False

    if not is_trusted_peer:
        return peer_host

    headers = connection.headers

    # 1. Cloudflare edge header (present when coming through Cloudflare Tunnel or proxied DNS)
    cf_ip = headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        try:
            clean_cf = cf_ip.strip("[]")
            if ":" in clean_cf and "." in clean_cf:
                clean_cf = clean_cf.split(":")[0]
            ipaddress.ip_address(clean_cf)
            return clean_cf
        except ValueError:
            pass

    # 2. Caddy X-Real-IP
    real_ip = headers.get("x-real-ip", "").strip()
    if real_ip:
        try:
            clean_real = real_ip.strip("[]")
            if ":" in clean_real and "." in clean_real:
                clean_real = clean_real.split(":")[0]
            ipaddress.ip_address(clean_real)
            return clean_real
        except ValueError:
            pass

    # 3. X-Forwarded-For (first valid entry)
    fwd = headers.get("x-forwarded-for", "").strip()
    if fwd:
        for candidate in fwd.split(","):
            cand = candidate.strip().strip("[]")
            if ":" in cand and "." in cand:
                cand = cand.split(":")[0]
            try:
                ipaddress.ip_address(cand)
                return cand
            except ValueError:
                continue

    return peer_host


def is_lan_client(connection: HTTPConnection, cfg: Optional[Settings] = None) -> bool:
    """
    Checks whether the client connection originates from a private/local LAN network.

    Security rules:
    1. If tagged as 'wan' by reverse proxy (e.g. Caddy public domain site block) -> False.
    2. If the request was addressed to the configured public domain (e.g. sightline.yourdomain.com) -> False.
       Public domain requests always require SSO authentication regardless of routing.
    3. Determine if the direct TCP peer is a trusted local reverse proxy (Caddy / Docker bridge / loopback).
       - If peer is trusted proxy: inspect X-Real-IP and X-Forwarded-For injected by Caddy.
       - If peer is NOT a trusted proxy (direct connection to port 8000): ignore all proxy headers to
         prevent spoofing, and inspect connection.client.host directly.
    4. Only return True if the extracted real client IP strictly belongs to a physical LAN subnet
       (192.168.0.0/16, 10.0.0.0/8) or loopback. Never blindly trust header tags without verified IP.
    """
    effective_cfg: Settings = cfg
    if not effective_cfg:
        app = getattr(connection, "app", None)
        if app and hasattr(app, "state"):
            effective_cfg = getattr(app.state, "settings", settings)
        else:
            effective_cfg = settings

    headers = connection.headers

    # 1. Reverse proxy explicit WAN tag from public domain site block
    access_tag = headers.get("x-sightline-access", "").strip().lower()
    if access_tag == "wan":
        return False

    # 2. Check if the request was sent to the configured public domain name
    if effective_cfg and effective_cfg.domain_name and effective_cfg.domain_name.strip():
        public_domain = effective_cfg.domain_name.strip().lower()
        if ":" in public_domain:
            public_domain = public_domain.split(":")[0]

        for h_key in ("x-forwarded-host", "host"):
            raw_host = headers.get(h_key, "").strip().lower()
            if raw_host:
                host_domain = raw_host.split(":")[0].strip()
                if host_domain == public_domain or host_domain.endswith("." + public_domain):
                    logger.debug(f"[auth] request to public domain {host_domain} enforced as WAN (no LAN bypass)")
                    return False

    # 3. Determine direct peer host and proxy trust
    peer_host = connection.client.host.strip() if connection.client and connection.client.host else ""
    is_trusted_peer = False
    if peer_host in ("testclient", "localhost", "127.0.0.1", "::1"):
        is_trusted_peer = True
    elif peer_host:
        try:
            peer_ip_obj = ipaddress.ip_address(peer_host.strip("[]"))
            for proxy_net in TRUSTED_PROXIES:
                if peer_ip_obj in proxy_net:
                    is_trusted_peer = True
                    break
        except ValueError:
            is_trusted_peer = False

    raw_ip = None
    if is_trusted_peer:
        # Trust proxy headers injected by Caddy / trusted gateway (X-Real-IP is overwritten with remote_host)
        if "x-real-ip" in headers:
            raw_ip = headers["x-real-ip"].strip()
        elif "x-forwarded-for" in headers:
            raw_ip = headers["x-forwarded-for"].split(",")[0].strip()
        else:
            raw_ip = peer_host
    else:
        # Direct connection on port 8000: disregard client-supplied proxy headers
        raw_ip = peer_host

    if not raw_ip:
        return False

    try:
        clean_ip = raw_ip.strip("[]")
        if ":" in clean_ip and "." in clean_ip:
            clean_ip = clean_ip.split(":")[0]
        ip = ipaddress.ip_address(clean_ip)

        if ip.is_loopback:
            return True

        for net in LAN_PRIVATE_NETWORKS:
            if ip in net:
                return True

        return False
    except ValueError:
        return False


def is_admin_user(user: Optional[User], cfg: Optional[Settings] = None) -> bool:
    """Checks whether the user has administrator privileges."""
    if not user or not user.email:
        return False
    if user.email in ("lan@sightline.local", "dev@sightline.local"):
        return True
    effective_cfg = cfg or settings
    if not effective_cfg.admin_google_emails:
        return False
    admin_emails = [e.lower() for e in effective_cfg.admin_google_emails]
    return user.email.lower() in admin_emails


def verify_google_token(
    token: str,
    allowed_emails: list[str],
    client_id: Optional[str] = None,
) -> User:
    """
    Verifies a Google ID token against Google's public keys and checks
    that the extracted email is on the allowed whitelist.
    """
    try:
        id_info = id_token.verify_oauth2_token(
            token,
            _google_request,
            audience=client_id,
        )
    except Exception as exc:
        logger.warning(f"[AUDIT] [AUTH_FAILURE] Google ID token verification failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google ID token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    email = str(id_info.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google ID token does not contain an email",
        )

    email_verified = id_info.get("email_verified")
    if email_verified is not True and str(email_verified).lower() != "true":
        logger.warning(f"[AUDIT] [AUTH_FAILURE] Rejected Google ID token with unverified email: {email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google account email is not verified",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if allowed_emails and email not in [e.lower() for e in allowed_emails]:
        logger.warning(f"[AUDIT] [AUTH_DENIED] Unauthorized email access attempt: {email}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied for {email}",
        )

    return User(
        email=email,
        name=id_info.get("name"),
        picture=id_info.get("picture"),
        iat=id_info.get("iat"),
    )


def verify_google_access_token(
    access_token: str,
    allowed_emails: list[str],
    client_id: Optional[str] = None,
) -> User:
    """
    Verifies a Google OAuth2 access token by querying Google's userinfo endpoint,
    validates the client_id audience via tokeninfo, and checks that the returned
    email is on the allowed whitelist.
    Enforces format pre-validation and aggressive 3.5s timeouts to prevent worker starvation.
    """
    token = access_token.strip()
    if not token or not is_plausible_google_access_token(token):
        logger.warning(f"[AUDIT] [AUTH_FAILURE] Malformed or invalid Google access token format")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google access token format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate that token was issued for our Google client_id to prevent cross-app substitution
    if client_id and client_id.strip():
        try:
            req_info = urllib.request.Request(
                f"https://oauth2.googleapis.com/tokeninfo?access_token={token}"
            )
            with urllib.request.urlopen(req_info, timeout=3.5) as resp_info:
                info_data = json.loads(resp_info.read().decode("utf-8"))
            aud = info_data.get("aud") or info_data.get("azp") or info_data.get("issued_to")
            if aud and aud != client_id.strip():
                logger.warning(f"[AUDIT] [AUTH_FAILURE] Google access token audience mismatch: {aud} != {client_id}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Google access token was not issued for this client application",
                )
        except urllib.error.HTTPError as e:
            logger.warning(f"[AUDIT] [AUTH_FAILURE] Google access token tokeninfo rejected: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired Google access token",
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(f"[AUDIT] [AUTH_FAILURE] Error checking access token audience: {exc}")

    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning(f"[AUDIT] [AUTH_FAILURE] Google access token rejected by Google API: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Google access token",
        )
    except Exception as exc:
        logger.warning(f"[AUDIT] [AUTH_FAILURE] Google access token verification error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Google authentication failed: {exc}",
        )

    email = str(data.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google account did not return an email address",
        )

    email_verified = data.get("email_verified")
    if email_verified is not True and str(email_verified).lower() != "true":
        logger.warning(f"[AUDIT] [AUTH_FAILURE] Rejected Google access token with unverified email: {email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google account email is not verified",
        )

    if allowed_emails and email not in [e.lower() for e in allowed_emails]:
        logger.warning(f"[AUDIT] [AUTH_DENIED] Unauthorized email access attempt via OAuth token: {email}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied for {email}",
        )

    return User(
        email=email,
        name=data.get("name"),
        picture=data.get("picture"),
    )


async def get_current_user(
    request: Request,
    auth: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> User:
    """
    FastAPI dependency to authenticate requests.
    Checks:
    1. Empty whitelist bypass (dev mode)
    2. LAN client bypass (if configured)
    3. Session cookie (sightline_session)
    4. HTTP Bearer authorization header (session token or Google ID token)
    """
    cfg: Settings = getattr(request.app.state, "settings", settings)
    is_lan = is_lan_client(request, cfg)

    if not cfg.allowed_google_emails:
        # Dev mode bypass is strictly restricted to local LAN / loopback clients
        if is_lan:
            return User(email="dev@sightline.local", name="Local Developer")
        logger.warning(f"[auth] rejected WAN request with empty user whitelist from {request.client}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required (no allowed user accounts configured)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if cfg.allow_lan_auth_bypass and is_lan:
        return User(email="lan@sightline.local", name="Local LAN User")

    secret = get_session_secret(cfg)
    db = getattr(request.app.state, "db", None)

    # 1. Check HttpOnly session cookie
    cookie_token = get_session_cookie_from_request(request)
    if cookie_token:
        user = verify_session_token(cookie_token, secret)
        if user and user.email:
            if db and hasattr(db, "is_token_revoked") and await db.is_token_revoked(hash_token(cookie_token)):
                logger.warning(f"[AUDIT] [TOKEN_REVOKED] Rejected revoked session cookie for {user.email}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session token has been revoked",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            user_revocations = getattr(request.app.state, "user_revocations", None) if hasattr(request, "app") and hasattr(request.app, "state") else None
            if is_user_session_revoked(user, user_revocations):
                logger.warning(f"[AUDIT] [TOKEN_REVOKED] Session cookie invalidated by user session revocation for {user.email}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session token has been revoked",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            email_lower = user.email.lower()
            allowed = [e.lower() for e in cfg.allowed_google_emails]
            if not allowed or email_lower in allowed:
                return user
            logger.warning(f"[AUDIT] [AUTH_DENIED] Session cookie email not authorized: {user.email}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied for {user.email}",
            )

    # 2. Check Bearer credentials in Authorization header
    raw_token = None
    if auth is not None and hasattr(auth, "credentials") and not callable(getattr(auth, "credentials", None)):
        try:
            raw_token = auth.credentials
        except AttributeError:
            raw_token = None

    if not raw_token and "authorization" in request.headers:
        hdr = request.headers["authorization"].strip()
        if hdr.lower().startswith("bearer "):
            raw_token = hdr[7:].strip()

    if raw_token:
        # Try HMAC session token first
        user = verify_session_token(raw_token, secret)
        if user and user.email:
            if db and hasattr(db, "is_token_revoked") and await db.is_token_revoked(hash_token(raw_token)):
                logger.warning(f"[AUDIT] [TOKEN_REVOKED] Rejected revoked bearer session token for {user.email}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session token has been revoked",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            user_revocations = getattr(request.app.state, "user_revocations", None) if hasattr(request, "app") and hasattr(request.app, "state") else None
            if is_user_session_revoked(user, user_revocations):
                logger.warning(f"[AUDIT] [TOKEN_REVOKED] Bearer session token invalidated by user session revocation for {user.email}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session token has been revoked",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            email_lower = user.email.lower()
            allowed = [e.lower() for e in cfg.allowed_google_emails]
            if not allowed or email_lower in allowed:
                return user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied for {user.email}",
            )

        # Fallback to validating Google OAuth2 ID token
        # Pre-validate JWT structure to prevent DoS against Google cert endpoints
        if not is_plausible_jwt(raw_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or malformed authentication token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user = verify_google_token(
            token=raw_token,
            allowed_emails=cfg.allowed_google_emails,
            client_id=cfg.google_client_id,
        )
        user_revocations = getattr(request.app.state, "user_revocations", None) if hasattr(request, "app") and hasattr(request.app, "state") else None
        if is_user_session_revoked(user, user_revocations):
            logger.warning(f"[AUDIT] [TOKEN_REVOKED] Google ID token invalidated by user session revocation for {user.email}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session token has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication credentials required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_optional_current_user(
    request: Request,
    auth: Optional[HTTPAuthorizationCredentials] = None,
) -> Optional[User]:
    """Returns the authenticated User if authenticated, or None if unauthenticated."""
    try:
        return await get_current_user(request, auth)
    except Exception:
        return None


_PUBLIC_REQ_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_PUBLIC_RATE_LIMIT_WINDOW = 60.0  # seconds
_MAX_PUBLIC_REQ_PER_WINDOW = 120   # per IP per minute
_MAX_PUBLIC_TRACKED_IPS = 5000


def check_public_rate_limit(request: Request, cfg: Optional[Settings] = None) -> None:
    """
    Enforces a sliding-window rate limit (120 req/min) for unauthenticated WAN clients
    on public endpoints (/ and /api/v1/auth/status) to mitigate bot scanning.
    LAN clients and authenticated sessions are completely exempt.
    """
    effective_cfg = cfg or getattr(request.app.state, "settings", settings)
    if is_lan_client(request, effective_cfg):
        return

    # Check for cryptographically valid session cookie or bearer token
    secret = get_session_secret(effective_cfg)
    cookie_token = get_session_cookie_from_request(request)
    if cookie_token and verify_session_token(cookie_token, secret):
        return

    auth_hdr = request.headers.get("authorization", "").strip()
    if auth_hdr.lower().startswith("bearer "):
        bearer_token = auth_hdr[7:].strip()
        if bearer_token and verify_session_token(bearer_token, secret):
            return

    client_ip = get_client_ip(request, effective_cfg)

    now = time.time()
    if len(_PUBLIC_REQ_ATTEMPTS) > 50:
        expired_ips = [
            ip for ip, timestamps in _PUBLIC_REQ_ATTEMPTS.items()
            if not timestamps or now - timestamps[-1] >= _PUBLIC_RATE_LIMIT_WINDOW
        ]
        for ip in expired_ips:
            _PUBLIC_REQ_ATTEMPTS.pop(ip, None)

    if len(_PUBLIC_REQ_ATTEMPTS) >= _MAX_PUBLIC_TRACKED_IPS and client_ip not in _PUBLIC_REQ_ATTEMPTS:
        oldest_ip = min(_PUBLIC_REQ_ATTEMPTS.keys(), key=lambda k: _PUBLIC_REQ_ATTEMPTS[k][-1] if _PUBLIC_REQ_ATTEMPTS[k] else 0)
        _PUBLIC_REQ_ATTEMPTS.pop(oldest_ip, None)

    attempts = _PUBLIC_REQ_ATTEMPTS[client_ip]
    valid_attempts = [t for t in attempts if now - t < _PUBLIC_RATE_LIMIT_WINDOW]
    if len(valid_attempts) >= _MAX_PUBLIC_REQ_PER_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again in a minute.",
            headers={"Retry-After": "60"},
        )
    valid_attempts.append(now)
    _PUBLIC_REQ_ATTEMPTS[client_ip] = valid_attempts


async def get_current_user_ws(
    cfg: Settings,
    websocket: Optional[HTTPConnection] = None,
) -> Optional[User]:
    is_lan = is_lan_client(websocket, cfg) if websocket else False

    if not cfg.allowed_google_emails:
        # Dev mode bypass is strictly restricted to local LAN / loopback clients
        if is_lan:
            return User(email="dev@sightline.local", name="Local Developer")
        logger.warning(
            f"[ws/auth] rejected WAN WebSocket connection with empty user whitelist from {websocket.client if websocket else 'unknown'}"
        )
        return None

    if cfg.allow_lan_auth_bypass and is_lan:
        return User(email="lan@sightline.local", name="Local LAN User")

    secret = get_session_secret(cfg)
    db = getattr(websocket.app.state, "db", None) if websocket and hasattr(websocket, "app") and hasattr(websocket.app, "state") else None
    user_revocations = getattr(websocket.app.state, "user_revocations", None) if websocket and hasattr(websocket, "app") and hasattr(websocket.app, "state") else None

    # 1. Check WebSocket cookies
    if websocket and hasattr(websocket, "cookies") and websocket.cookies:
        cookie_token = get_session_cookie_from_request(websocket)
        if cookie_token:
            user = verify_session_token(cookie_token, secret)
            if user and user.email:
                if db and hasattr(db, "is_token_revoked") and await db.is_token_revoked(hash_token(cookie_token)):
                    logger.warning(f"[AUDIT] [TOKEN_REVOKED] [ws] Rejected revoked session cookie for {user.email}")
                    return None
                if is_user_session_revoked(user, user_revocations):
                    logger.warning(f"[AUDIT] [TOKEN_REVOKED] [ws] Session cookie invalidated by user revocation for {user.email}")
                    return None
                email_lower = user.email.lower()
                allowed = [e.lower() for e in cfg.allowed_google_emails]
                if not allowed or email_lower in allowed:
                    return user

    # 2. Check WebSocket Authorization header
    if websocket and hasattr(websocket, "headers") and "authorization" in websocket.headers:
        hdr = websocket.headers["authorization"].strip()
        if hdr.lower().startswith("bearer "):
            bearer_token = hdr[7:].strip()
            user = verify_session_token(bearer_token, secret)
            if user and user.email:
                if db and hasattr(db, "is_token_revoked") and await db.is_token_revoked(hash_token(bearer_token)):
                    logger.warning(f"[AUDIT] [TOKEN_REVOKED] [ws] Rejected revoked bearer token for {user.email}")
                    return None
                if is_user_session_revoked(user, user_revocations):
                    logger.warning(f"[AUDIT] [TOKEN_REVOKED] [ws] Bearer token invalidated by user revocation for {user.email}")
                    return None
                email_lower = user.email.lower()
                allowed = [e.lower() for e in cfg.allowed_google_emails]
                if not allowed or email_lower in allowed:
                    return user

            if is_plausible_jwt(bearer_token):
                try:
                    user = verify_google_token(
                        token=bearer_token,
                        allowed_emails=cfg.allowed_google_emails,
                        client_id=cfg.google_client_id,
                    )
                    if is_user_session_revoked(user, user_revocations):
                        logger.warning(f"[AUDIT] [TOKEN_REVOKED] [ws] Google ID token invalidated by user revocation for {user.email}")
                        return None
                    return user
                except Exception:
                    return None

    # 3. Check Sec-WebSocket-Protocol subprotocol (fallback for browsers blocking cookies on wss)
    if websocket and hasattr(websocket, "headers") and "sec-websocket-protocol" in websocket.headers:
        raw_proto = websocket.headers["sec-websocket-protocol"]
        parts = [p.strip() for p in raw_proto.split(",") if p.strip()]
        token_candidate = None
        if len(parts) >= 2 and parts[0] == "sightline.auth":
            token_candidate = parts[1]
        elif len(parts) == 1 and parts[0] != "sightline.auth":
            token_candidate = parts[0]

        if token_candidate:
            user = verify_session_token(token_candidate, secret)
            if user and user.email:
                if db and hasattr(db, "is_token_revoked") and await db.is_token_revoked(hash_token(token_candidate)):
                    logger.warning(f"[AUDIT] [TOKEN_REVOKED] [ws] Rejected revoked subprotocol session token for {user.email}")
                    return None
                if is_user_session_revoked(user, user_revocations):
                    logger.warning(f"[AUDIT] [TOKEN_REVOKED] [ws] Subprotocol token invalidated by user revocation for {user.email}")
                    return None
                email_lower = user.email.lower()
                allowed = [e.lower() for e in cfg.allowed_google_emails]
                if not allowed or email_lower in allowed:
                    return user

            if is_plausible_jwt(token_candidate):
                try:
                    user = verify_google_token(
                        token=token_candidate,
                        allowed_emails=cfg.allowed_google_emails,
                        client_id=cfg.google_client_id,
                    )
                    if is_user_session_revoked(user, user_revocations):
                        logger.warning(f"[AUDIT] [TOKEN_REVOKED] [ws] Google ID token invalidated by user revocation for {user.email}")
                        return None
                    return user
                except Exception:
                    return None

    return None
