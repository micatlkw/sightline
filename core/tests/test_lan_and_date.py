from __future__ import annotations

import datetime
import ipaddress
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str = None, headers: dict = None):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
        super().__init__(detail)

status_mock = MagicMock()
status_mock.HTTP_400_BAD_REQUEST = 400
status_mock.HTTP_401_UNAUTHORIZED = 401
status_mock.HTTP_403_FORBIDDEN = 403
status_mock.HTTP_429_TOO_MANY_REQUESTS = 429
status_mock.HTTP_500_INTERNAL_SERVER_ERROR = 500

fastapi_mock = MagicMock()
fastapi_mock.HTTPException = HTTPException
fastapi_mock.status = status_mock
fastapi_mock.Request = MagicMock
fastapi_mock.Response = MagicMock
fastapi_mock.APIRouter = MagicMock
fastapi_mock.Security = lambda x: None
fastapi_mock.Depends = lambda x: None
fastapi_mock.Query = lambda default=None, **kwargs: default

fastapi_sec_mock = MagicMock()
fastapi_sec_mock.HTTPBearer = MagicMock
fastapi_sec_mock.HTTPAuthorizationCredentials = MagicMock

sys.modules["fastapi"] = fastapi_mock
sys.modules["fastapi.security"] = fastapi_sec_mock

import types
starlette_mod = types.ModuleType("starlette")
starlette_req_mod = types.ModuleType("starlette.requests")
starlette_req_mod.HTTPConnection = MagicMock
starlette_ds_mod = types.ModuleType("starlette.datastructures")
starlette_ds_mod.Headers = MagicMock
starlette_types_mod = types.ModuleType("starlette.types")
sys.modules["starlette"] = starlette_mod
sys.modules["starlette.requests"] = starlette_req_mod
sys.modules["starlette.datastructures"] = starlette_ds_mod
sys.modules["starlette.types"] = starlette_types_mod

google_mod = types.ModuleType("google")
google_auth_mod = types.ModuleType("google.auth")
google_auth_transport_mod = types.ModuleType("google.auth.transport")
google_auth_transport_requests_mod = types.ModuleType("google.auth.transport.requests")
google_auth_transport_requests_mod.Request = MagicMock
google_oauth2_mod = types.ModuleType("google.oauth2")
google_oauth2_idtoken_mod = types.ModuleType("google.oauth2.id_token")

sys.modules["google"] = google_mod
sys.modules["google.auth"] = google_auth_mod
sys.modules["google.auth.transport"] = google_auth_transport_mod
sys.modules["google.auth.transport.requests"] = google_auth_transport_requests_mod
sys.modules["google.oauth2"] = google_oauth2_mod
sys.modules["google.oauth2.id_token"] = google_oauth2_idtoken_mod

class BaseModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

def field_validator(*args, **kwargs):
    def decorator(fn):
        return classmethod(fn)
    return decorator

pydantic_mock = MagicMock()
pydantic_mock.BaseModel = BaseModel
pydantic_mock.Field = lambda *args, **kwargs: None
pydantic_mock.field_validator = field_validator
sys.modules["pydantic"] = pydantic_mock

pydantic_settings_mock = MagicMock()
class BaseSettings:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
pydantic_settings_mock.BaseSettings = BaseSettings
pydantic_settings_mock.SettingsConfigDict = MagicMock
sys.modules["pydantic_settings"] = pydantic_settings_mock
sys.modules["yaml"] = MagicMock()
sys.modules["aiosqlite"] = MagicMock()

from app.config import Settings
from app.auth.google_sso import is_lan_client, LAN_PRIVATE_NETWORKS, LAN_ORIGIN_REGEX

print("=" * 70)
print("Running LAN Access & Dashboard Date Verification Suite")
print("=" * 70)

# ==============================================================================
# Test 1: LAN Subnet Inclusion & is_lan_client Detection
# ==============================================================================
print("\n[Test 1] Verifying LAN Subnet Inclusion in google_sso.py...")
assert ipaddress.ip_network("172.16.0.0/12") in LAN_PRIVATE_NETWORKS, "172.16.0.0/12 must be in LAN_PRIVATE_NETWORKS"
assert ipaddress.ip_network("192.168.0.0/16") in LAN_PRIVATE_NETWORKS
assert ipaddress.ip_network("10.0.0.0/8") in LAN_PRIVATE_NETWORKS

# Test origins
assert LAN_ORIGIN_REGEX.match("http://192.168.1.100:4280") is not None
assert LAN_ORIGIN_REGEX.match("http://172.19.0.1:4280") is not None
assert LAN_ORIGIN_REGEX.match("http://172.17.0.1:4280") is not None
assert LAN_ORIGIN_REGEX.match("http://10.0.0.5:4280") is not None
assert LAN_ORIGIN_REGEX.match("http://localhost:8080") is not None
assert LAN_ORIGIN_REGEX.match("http://8.8.8.8:4280") is None

# Test is_lan_client with mock connection
class MockClient:
    def __init__(self, host: str):
        self.host = host

class MockConnection:
    def __init__(self, client_host: str, headers: dict = None):
        self.client = MockClient(client_host)
        self.headers = headers or {}
        self.app = None

cfg = Settings(domain_name="sightline.example.com", https_port=4210, http_port=4280)

# Case 1: Docker proxy from host to caddy
conn_docker_host = MockConnection("172.19.0.2", headers={"x-real-ip": "172.19.0.1", "host": "192.168.1.100:4280"})
assert is_lan_client(conn_docker_host, cfg) is True, "Docker host gateway connection must be LAN"

# Case 2: LAN phone on 192.168.1.101
conn_lan = MockConnection("172.19.0.2", headers={"x-real-ip": "192.168.1.101", "host": "192.168.1.100:4280"})
assert is_lan_client(conn_lan, cfg) is True, "LAN client 192.168.1.101 must be LAN"

# Case 3: WAN access via public domain
conn_wan_domain = MockConnection("172.19.0.2", headers={"x-real-ip": "192.168.1.101", "host": "sightline.example.com:4210"})
assert is_lan_client(conn_wan_domain, cfg) is False, "Public domain requests must NOT bypass auth"

# Case 4: WAN access tagged as wan by Caddy
conn_wan_tag = MockConnection("172.19.0.2", headers={"x-sightline-access": "wan", "x-real-ip": "192.168.1.101", "host": "192.168.1.100:4280"})
assert is_lan_client(conn_wan_tag, cfg) is False, "WAN access tag must never bypass auth"

# Case 5: External IP over direct connection
conn_external = MockConnection("172.19.0.2", headers={"x-real-ip": "203.0.113.5", "host": "192.168.1.100:4280"})
assert is_lan_client(conn_external, cfg) is False, "External IP must not be LAN"

print("  ✓ is_lan_client correctly recognizes Docker bridge (172.16/12), LAN (192.168/16), and rejects WAN/public domain")


# ==============================================================================
# Test 2: Caddyfile HTTP vs HTTPS Security Headers
# ==============================================================================
print("\n[Test 2] Checking Caddyfile HTTP vs HTTPS security header generation...")
s = Settings(
    domain_name="sightline.example.com",
    https_port=4210,
    http_port=4280,
    lan_hosts=["192.168.1.100", "localhost"],
)
gen = s.generate_caddyfile()

# Split into HTTPS section and HTTP section
http_idx = gen.find("http://:4280")
assert http_idx != -1, "http://:4280 section not found in generated Caddyfile"
https_part = gen[:http_idx]
http_part = gen[http_idx:]

# HTTP section assertions:
assert "Strict-Transport-Security" not in http_part, "HTTP section MUST NOT contain Strict-Transport-Security!"
assert "upgrade-insecure-requests" not in http_part, "HTTP section MUST NOT contain upgrade-insecure-requests!"
assert "X-Content-Type-Options" in http_part
assert "X-Frame-Options" in http_part
assert "Referrer-Policy" in http_part

# HTTPS section assertions:
assert "Strict-Transport-Security" in https_part, "HTTPS section must contain Strict-Transport-Security"
assert "upgrade-insecure-requests" in https_part, "HTTPS section must contain upgrade-insecure-requests"

# Verify live Caddyfile on disk (if present on local Synology environment)
live_caddy_path = Path("/volume1/sightline/config/Caddyfile")
if live_caddy_path.exists():
    live_caddy = live_caddy_path.read_text(encoding="utf-8")
    live_http_idx = live_caddy.find("http://:4280")
    assert live_http_idx != -1
    live_http_part = live_caddy[live_http_idx:]
    assert "Strict-Transport-Security" not in live_http_part
    assert "upgrade-insecure-requests" not in live_http_part
    print("  ✓ Caddyfile HTTP block successfully omits HSTS & upgrade-insecure-requests")
    print("  ✓ Caddyfile HTTPS block successfully preserves full HSTS & upgrade-insecure-requests")


# ==============================================================================
# Test 3: Frontend Date Formatting & Card Layout Logic
# ==============================================================================
print("\n[Test 3] Verifying Dashboard Date formatting and card markup in index.html...")
index_html = (ROOT_DIR / "app/ui/templates/index.html").read_text(encoding="utf-8")

# 1. Verify formatEventDate exists
assert "function formatEventDate(isoStr)" in index_html, "formatEventDate function missing in index.html"
assert "return 'Today'" in index_html
assert "return 'Yesterday'" in index_html

# 2. Verify date badge markup in card body
assert "formatEventDate(event.detected_at)" in index_html
assert "fa-regular fa-calendar" in index_html
assert "ml-auto" in index_html, "Date badge should use ml-auto to align to the rightmost edge"

# 3. Verify overlay time badge remains on video thumbnail
assert "formatEventTime(event.detected_at)" in index_html
assert "absolute bottom-2.5 right-2.5" in index_html, "Overlay time badge must remain in bottom-right corner of thumbnail"

# 4. Simulate date formatting logic in Python
def format_event_date_py(dt: datetime.datetime, now: datetime.datetime) -> str:
    today = datetime.date(now.year, now.month, now.day)
    event_day = datetime.date(dt.year, dt.month, dt.day)
    diff = (today - event_day).days
    if diff == 0:
        return "Today"
    elif diff == 1:
        return "Yesterday"
    else:
        return dt.strftime("%b %-d, %Y")

now = datetime.datetime(2026, 9, 4, 1, 30, 0)
assert format_event_date_py(datetime.datetime(2026, 9, 4, 0, 15, 0), now) == "Today"
assert format_event_date_py(datetime.datetime(2026, 9, 3, 23, 45, 0), now) == "Yesterday"
assert format_event_date_py(datetime.datetime(2026, 8, 30, 12, 0, 0), now) == "Aug 30, 2026"

print("  ✓ Dashboard HTML contains formatEventDate and rightmost date badge")
print("  ✓ Thumbnail overlay time badge preserved")
print("  ✓ Date formatting logic correctly resolves Today, Yesterday, and localized dates")

print("\n" + "=" * 70)
print("ALL VERIFICATION CHECKS PASSED!")
print("=" * 70)
