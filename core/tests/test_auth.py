from unittest.mock import patch
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth.google_sso import verify_google_token
from app.config import Settings
from app.main import create_app


def test_verify_google_token_allowed():
    fake_payload = {"email": "alice@gmail.com", "name": "Alice Smith"}
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_payload):
        user = verify_google_token("valid-token", allowed_emails=["alice@gmail.com", "bob@gmail.com"])
        assert user.email == "alice@gmail.com"
        assert user.name == "Alice Smith"


def test_verify_google_token_forbidden():
    fake_payload = {"email": "stranger@gmail.com"}
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_payload):
        with pytest.raises(HTTPException) as exc:
            verify_google_token("valid-token", allowed_emails=["alice@gmail.com"])
        assert exc.value.status_code == 403


def test_verify_google_token_invalid_signature():
    with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Token expired")):
        with pytest.raises(HTTPException) as exc:
            verify_google_token("expired-token", allowed_emails=["alice@gmail.com"])
        assert exc.value.status_code == 401


def test_auth_middleware_bypass_when_empty_whitelist(test_settings: Settings):
    test_settings.allowed_google_emails = []
    app = create_app(test_settings)
    with TestClient(app) as client:
        res = client.get("/events")
        assert res.status_code == 200


def test_auth_middleware_enforced_when_whitelist_set(test_settings: Settings):
    test_settings.allowed_google_emails = ["family@gmail.com"]
    app = create_app(test_settings)
    with TestClient(app) as client:
        # Missing auth header -> 401
        res = client.get("/events")
        assert res.status_code == 401

        # Valid auth header -> 200
        fake_payload = {"email": "family@gmail.com"}
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_payload):
            res = client.get("/events", headers={"Authorization": "Bearer good-token"})
            assert res.status_code == 200


def test_auth_middleware_lan_bypass_allowed(test_settings: Settings):
    test_settings.allowed_google_emails = ["family@gmail.com"]
    test_settings.allow_lan_auth_bypass = True
    app = create_app(test_settings)
    with TestClient(app) as client:
        # Request with private LAN IP in X-Real-IP -> 200 without token
        res_lan = client.get("/events", headers={"X-Real-IP": "192.168.1.100"})
        assert res_lan.status_code == 200

        # Request with 10.x.x.x in X-Forwarded-For -> 200 without token
        res_lan10 = client.get("/events", headers={"X-Forwarded-For": "10.0.0.5, 172.18.0.1"})
        assert res_lan10.status_code == 200

        # Request from public WAN IP -> 401
        res_wan = client.get("/events", headers={"X-Real-IP": "203.0.113.50"})
        assert res_wan.status_code == 401

        # Request addressed to configured public domain -> 401 (never bypassed as LAN)
        test_settings.domain_name = "cam.example.com"
        res_domain = client.get("/events", headers={"Host": "cam.example.com", "X-Real-IP": "192.168.1.100"})
        assert res_domain.status_code == 401

        # Request via reverse proxy with X-Sightline-Access: wan -> 401
        res_tag_wan = client.get("/events", headers={"X-Sightline-Access": "wan", "X-Real-IP": "192.168.1.100"})
        assert res_tag_wan.status_code == 401

        # Request with Docker bridge gateway IP (172.18.0.1) -> 401 (not physical LAN)
        res_docker = client.get("/events", headers={"X-Real-IP": "172.18.0.1"})
        assert res_docker.status_code == 401


def test_auth_middleware_lan_bypass_disabled(test_settings: Settings):
    test_settings.allowed_google_emails = ["family@gmail.com"]
    test_settings.allow_lan_auth_bypass = False
    app = create_app(test_settings)
    with TestClient(app) as client:
        # Request with private LAN IP when bypass disabled -> 401
        res_lan = client.get("/events", headers={"X-Real-IP": "192.168.1.100"})
        assert res_lan.status_code == 401


def test_auth_status_endpoint(test_settings: Settings):
    test_settings.allowed_google_emails = ["admin@example.com"]
    test_settings.google_client_id = "test-client-id.apps.googleusercontent.com"
    test_settings.domain_name = "cam.example.com"
    app = create_app(test_settings)
    with TestClient(app) as client:
        # 1. LAN request
        res = client.get("/api/v1/auth/status", headers={"X-Real-IP": "192.168.1.100"})
        assert res.status_code == 200
        data = res.json()
        assert data["auth_enabled"] is True
        assert data["google_client_id"] == "test-client-id.apps.googleusercontent.com"
        assert data["authenticated"] is True
        assert data["lan_bypass_active"] is True

        # 2. WAN request to public domain -> not authenticated, no LAN bypass
        res_wan = client.get("/api/v1/auth/status", headers={"Host": "cam.example.com", "X-Real-IP": "192.168.1.100"})
        assert res_wan.status_code == 200
        data_wan = res_wan.json()
        assert data_wan["authenticated"] is False
        assert data_wan["lan_bypass_active"] is False



def test_auth_login_and_session_cookie_flow(test_settings: Settings):
    test_settings.allowed_google_emails = ["allowed@gmail.com"]
    test_settings.allow_lan_auth_bypass = False
    app = create_app(test_settings)
    with TestClient(app) as client:
        # 1. Login with unauthorized email -> 403
        bad_payload = {"email": "stranger@gmail.com", "name": "Stranger"}
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=bad_payload):
            res_bad = client.post("/api/v1/auth/login", json={"credential": "token-stranger"})
            assert res_bad.status_code == 403

        # 2. Login with authorized email -> 200 + set-cookie
        good_payload = {"email": "allowed@gmail.com", "name": "Allowed User", "picture": "https://avatar.png"}
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=good_payload):
            res_login = client.post("/api/v1/auth/login", json={"credential": "token-allowed"})
            assert res_login.status_code == 200
            assert "sightline_session" in res_login.cookies
            assert res_login.json()["user"]["email"] == "allowed@gmail.com"

            # 3. Access protected route using the established session cookie
            res_protected = client.get("/events")
            assert res_protected.status_code == 200

            # 4. Status endpoint now reflects authenticated user
            res_status = client.get("/api/v1/auth/status")
            assert res_status.status_code == 200
            assert res_status.json()["authenticated"] is True
            assert res_status.json()["user"]["email"] == "allowed@gmail.com"

            # 5. Logout clears the cookie
            res_logout = client.post("/api/v1/auth/logout")
            assert res_logout.status_code == 200
            assert res_logout.json()["status"] == "logged_out"


def test_auth_login_with_access_token(test_settings: Settings):
    test_settings.allowed_google_emails = ["user@example.com"]
    test_settings.allow_lan_auth_bypass = False
    app = create_app(test_settings)
    with TestClient(app) as client:
        with patch("app.api.routes.auth.verify_google_access_token") as mock_verify:
            from app.auth.google_sso import User
            mock_verify.return_value = User(email="user@example.com", name="OAuth User")
            res = client.post("/api/v1/auth/login", json={"access_token": "ya29.test-access-token"})
            assert res.status_code == 200
            assert "sightline_session" in res.cookies
            assert res.json()["user"]["email"] == "user@example.com"


def test_websocket_origin_validation(test_settings: Settings):
    from starlette.websockets import WebSocketDisconnect
    test_settings.allowed_google_emails = []  # dev mode
    test_settings.domain_name = "cam.example.com"
    app = create_app(test_settings)
    with TestClient(app) as client:
        # 1. Untrusted cross-origin -> rejected with policy violation (1008)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws", headers={"Origin": "https://evil.com"}):
                pass
        assert exc.value.code == 1008

        # 2. Trusted domain origin -> accepted
        with client.websocket_connect("/ws", headers={"Origin": "https://cam.example.com"}):
            pass


def test_direct_port_8000_header_spoofing_prevented(test_settings: Settings):
    from starlette.requests import Request
    from app.auth.google_sso import is_lan_client

    test_settings.allowed_google_emails = ["owner@gmail.com"]
    test_settings.allow_lan_auth_bypass = True

    # Mock connection where direct TCP peer is a public external IP (203.0.113.5)
    scope = {
        "type": "http",
        "client": ("203.0.113.5", 54321),
        "headers": [
            (b"x-sightline-access", b"lan"),
            (b"x-forwarded-for", b"192.168.1.100"),
            (b"cf-connecting-ip", b"10.0.0.1"),
            (b"x-real-ip", b"192.168.1.50"),
        ],
    }
    req = Request(scope)
    # Directly connecting untrusted peer with fake proxy headers must NOT be recognized as LAN
    assert is_lan_client(req, test_settings) is False


def test_session_secret_ephemeral_fallback(tmp_path: Path):
    from app.auth.google_sso import get_session_secret
    # Point db_path to a non-creatable path
    unwriteable_dir = tmp_path / "non_existing_parent" / "nested" / "test.db"
    # Create a non-directory file to block mkdir
    blocker = tmp_path / "non_existing_parent"
    blocker.write_text("file")
    s = Settings(db_path=unwriteable_dir)
    secret = get_session_secret(s)
    assert isinstance(secret, str)
    assert len(secret) == 64
    assert secret != "sightline-session-secret-fallback-key"


def test_auth_status_does_not_leak_session_token(test_settings: Settings):
    test_settings.allowed_google_emails = ["admin@example.com"]
    app = create_app(test_settings)
    with TestClient(app) as client:
        res = client.get("/api/v1/auth/status", headers={"X-Real-IP": "192.168.1.100"})
        assert res.status_code == 200
        data = res.json()
        assert data["authenticated"] is True
        # Verify token is None (no token leakage in status JSON)
        assert data.get("token") is None


def test_login_rate_limiting(test_settings: Settings):
    from app.api.routes.auth import _LOGIN_ATTEMPTS
    _LOGIN_ATTEMPTS.clear()
    test_settings.allowed_google_emails = ["user@example.com"]
    app = create_app(test_settings)
    with TestClient(app) as client:
        # 10 failed or rapid login attempts from the same IP
        for _ in range(10):
            res = client.post("/api/v1/auth/login", json={"credential": "invalid-token"}, headers={"X-Real-IP": "198.51.100.1"})
            assert res.status_code != 429

        # The 11th attempt must be rate-limited with 429 Too Many Requests
        res_limit = client.post("/api/v1/auth/login", json={"credential": "invalid-token"}, headers={"X-Real-IP": "198.51.100.1"})
        assert res_limit.status_code == 429


def test_session_token_revocation_on_logout(test_settings: Settings):
    from app.auth.google_sso import (
        SESSION_COOKIE_NAME,
        User,
        create_session_token,
        get_session_secret,
    )

    test_settings.allowed_google_emails = ["user@example.com"]
    test_settings.allow_lan_auth_bypass = False
    app = create_app(test_settings)
    secret = get_session_secret(test_settings)
    token = create_session_token(User(email="user@example.com"), secret)

    with TestClient(app) as client:
        # 1. Authenticated with valid token -> 200 OK
        client.cookies.set(SESSION_COOKIE_NAME, token)
        res = client.get("/api/v1/auth/me")
        assert res.status_code == 200
        assert res.json()["email"] == "user@example.com"

        # 2. Call /api/v1/auth/logout -> 200 OK
        res_logout = client.post("/api/v1/auth/logout")
        assert res_logout.status_code == 200

        # 3. Manually re-send the revoked token -> 401 Unauthorized
        client.cookies.set(SESSION_COOKIE_NAME, token)
        res_after = client.get("/api/v1/auth/me")
        assert res_after.status_code == 401
        assert "revoked" in res_after.json()["detail"].lower()


def test_jwt_plausible_fast_fail(test_settings: Settings):
    from app.auth.google_sso import is_plausible_jwt

    assert is_plausible_jwt("not-a-token") is False
    assert is_plausible_jwt("a.b") is False
    assert is_plausible_jwt("a.b.c.d") is False
    # Malformed base64
    assert is_plausible_jwt("???.???.???") is False

    test_settings.allowed_google_emails = ["user@example.com"]
    test_settings.allow_lan_auth_bypass = False
    app = create_app(test_settings)
    with TestClient(app) as client:
        # Garbage bearer token should immediately return 401 without hitting Google
        res = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-valid-jwt-token"})
        assert res.status_code == 401


def test_strict_admin_privilege_evaluation():
    from app.auth.google_sso import User, is_admin_user

    # When admin_google_emails is empty, viewers are NOT admins
    cfg_no_admin = Settings(
        allowed_google_emails=["viewer@example.com"],
        admin_google_emails=[],
    )
    viewer = User(email="viewer@example.com")
    assert is_admin_user(viewer, cfg_no_admin) is False

    # Dev and LAN users retain admin privileges
    assert is_admin_user(User(email="lan@sightline.local"), cfg_no_admin) is True
    assert is_admin_user(User(email="dev@sightline.local"), cfg_no_admin) is True

    # Explicit admin user is recognized
    cfg_with_admin = Settings(
        allowed_google_emails=["viewer@example.com", "admin@example.com"],
        admin_google_emails=["admin@example.com"],
    )
    admin = User(email="admin@example.com")
    assert is_admin_user(admin, cfg_with_admin) is True
    assert is_admin_user(viewer, cfg_with_admin) is False


def test_lan_client_spoofed_headers_ignored():
    from starlette.requests import Request
    from app.auth.google_sso import is_lan_client

    cfg = Settings(
        allowed_google_emails=["admin@example.com"],
        allow_lan_auth_bypass=True,
    )

    # Trusted proxy connection sending external X-Real-IP but spoofed CF-Connecting-IP
    scope = {
        "type": "http",
        "client": ("127.0.0.1", 50000),
        "headers": [
            (b"x-real-ip", b"203.0.113.50"),
            (b"cf-connecting-ip", b"192.168.1.100"),
            (b"true-client-ip", b"192.168.1.100"),
            (b"x-sightline-access", b"lan"),
        ],
    }
    req = Request(scope)
    # Must NOT evaluate to LAN because X-Real-IP is a public WAN IP
    assert is_lan_client(req, cfg) is False


def test_google_oauth_email_verified_required():
    from unittest.mock import patch
    from fastapi import HTTPException
    from app.auth.google_sso import verify_google_token, verify_google_access_token

    # 1. verify_google_token with email_verified: False -> 401
    with patch("google.oauth2.id_token.verify_oauth2_token") as mock_id:
        mock_id.return_value = {"email": "admin@example.com", "email_verified": False}
        with pytest.raises(HTTPException) as exc_info:
            verify_google_token("fake-token", allowed_emails=["admin@example.com"])
        assert exc_info.value.status_code == 401
        assert "not verified" in exc_info.value.detail.lower()

    # 2. verify_google_token with email_verified missing -> 401
    with patch("google.oauth2.id_token.verify_oauth2_token") as mock_id:
        mock_id.return_value = {"email": "admin@example.com"}
        with pytest.raises(HTTPException) as exc_info:
            verify_google_token("fake-token", allowed_emails=["admin@example.com"])
        assert exc_info.value.status_code == 401
        assert "not verified" in exc_info.value.detail.lower()

    # 3. verify_google_token with email_verified: True -> 200 / User
    with patch("google.oauth2.id_token.verify_oauth2_token") as mock_id:
        mock_id.return_value = {"email": "admin@example.com", "email_verified": True}
        user = verify_google_token("fake-token", allowed_emails=["admin@example.com"])
        assert user.email == "admin@example.com"

    # 4. verify_google_access_token with email_verified: False -> 401
    with patch("urllib.request.urlopen") as mock_urlopen:
        from io import BytesIO
        resp = BytesIO(b'{"email": "admin@example.com", "email_verified": false}')
        mock_urlopen.return_value.__enter__.return_value = resp
        with pytest.raises(HTTPException) as exc_info:
            verify_google_access_token("fake-access", allowed_emails=["admin@example.com"])
        assert exc_info.value.status_code == 401
        assert "not verified" in exc_info.value.detail.lower()


def test_rfc6265bis_cookie_deletion_on_logout(test_settings: Settings):
    from app.auth.google_sso import SESSION_COOKIE_NAME, User, create_session_token, get_session_secret

    test_settings.allowed_google_emails = ["admin@example.com"]
    app = create_app(test_settings)
    secret = get_session_secret(test_settings)
    token = create_session_token(User(email="admin@example.com"), secret)

    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE_NAME, token)
        res = client.post("/api/v1/auth/logout", headers={"X-Forwarded-Proto": "https"})
        assert res.status_code == 200
        set_cookie = res.headers.get("set-cookie", "")
        # Under HTTPS, must include secure and httponly on deletion for RFC 6265bis
        assert "secure" in set_cookie.lower()
        assert "httponly" in set_cookie.lower()


def test_login_rate_limiter_memory_pruning():
    import time
    from app.api.routes.auth import check_login_rate_limit, _LOGIN_ATTEMPTS

    _LOGIN_ATTEMPTS.clear()

    # Simulate 60 old IP entries that expired 120s ago
    old_time = time.time() - 120.0
    for i in range(60):
        _LOGIN_ATTEMPTS[f"10.0.0.{i}"] = [old_time]

    assert len(_LOGIN_ATTEMPTS) == 60

    # New login check should prune expired entries
    check_login_rate_limit("198.51.100.99")
    # All 60 expired IPs should be pruned, leaving only the new active IP
    assert len(_LOGIN_ATTEMPTS) <= 5
    assert "198.51.100.99" in _LOGIN_ATTEMPTS


def test_ws_origin_validation():
    from app.auth.google_sso import is_allowed_ws_origin
    cfg = Settings(domain_name="sightline.example.com", https_port=4210, http_port=4280)
    assert not is_allowed_ws_origin("https://evil.com", cfg)
    assert not is_allowed_ws_origin("https://attacker.org:4210", cfg)
    assert is_allowed_ws_origin("https://sightline.example.com:4210", cfg)
    assert is_allowed_ws_origin("https://sightline.example.com", cfg)
    assert is_allowed_ws_origin("http://192.168.1.100:4210", cfg)
    assert is_allowed_ws_origin("http://localhost", cfg)
    assert is_allowed_ws_origin("capacitor://localhost", cfg)
    assert is_allowed_ws_origin(None, cfg)


def test_wan_empty_whitelist_fails_closed(test_settings: Settings):
    test_settings.allowed_google_emails = []
    app = create_app(test_settings)
    with TestClient(app) as client:
        # WAN request with empty allowed emails fails closed (401)
        res = client.get("/api/v1/events", headers={"X-Sightline-Access": "wan", "X-Real-IP": "203.0.113.5"})
        assert res.status_code == 401
        assert "Authentication required" in res.text

        # LAN request with empty allowed emails gets dev mode bypass
        res_lan = client.get("/api/v1/events", headers={"X-Real-IP": "192.168.1.100"})
        assert res_lan.status_code == 200


def test_auth_status_masks_lan_flags_on_wan(test_settings: Settings):
    test_settings.allowed_google_emails = ["admin@example.com"]
    app = create_app(test_settings)
    with TestClient(app) as client:
        # Unauthenticated WAN request
        res_wan = client.get("/api/v1/auth/status", headers={"X-Sightline-Access": "wan", "X-Real-IP": "203.0.113.5"})
        assert res_wan.status_code == 200
        data_wan = res_wan.json()
        assert data_wan["allow_lan_auth_bypass"] is False
        assert data_wan["lan_bypass_active"] is False
        assert data_wan["is_lan"] is False

        # LAN request
        res_lan = client.get("/api/v1/auth/status", headers={"X-Real-IP": "192.168.1.100"})
        assert res_lan.status_code == 200
        data_lan = res_lan.json()
        assert data_lan["is_lan"] is True




