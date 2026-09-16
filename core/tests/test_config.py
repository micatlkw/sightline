from pathlib import Path
from app.config import Settings


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.incoming_dir == Path("/data/incoming")
    assert s.processed_dir == Path("/data/processed")
    assert s.watch_dir == Path("/data/incoming")
    assert s.yolo_model == "yolo11n.pt"
    assert s.target_classes == [0, 2, 3, 7, 15, 16, 21]
    assert s.confidence_threshold == 0.45
    assert s.vid_stride == 30
    assert s.watch_use_polling is False
    assert s.clip_stable_seconds == 2.0
    assert s.scan_on_startup is True
    assert s.allow_lan_auth_bypass is True
    assert s.alert_cooldown_seconds == 60


def test_settings_scan_on_startup_override():
    s = Settings(scan_on_startup=False)
    assert s.scan_on_startup is False


def test_settings_allow_lan_auth_bypass_override():
    s = Settings(allow_lan_auth_bypass=False)
    assert s.allow_lan_auth_bypass is False


def test_settings_json_string_parsing():
    s = Settings(
        target_classes="[0, 16]",
        apprise_urls='["ntfys://ntfy.sh/demo-alerts?priority=high&tags=rotating_light"]',
        allowed_google_emails='["User.Name@Example.COM"]',
        admin_google_emails='["Admin.User@Example.COM"]',
    )
    assert s.target_classes == [0, 16]
    # apprise_urls preserves case for topics / tokens
    assert s.apprise_urls == ["ntfys://ntfy.sh/demo-alerts?priority=high&tags=rotating_light"]
    # emails are normalized to lowercase
    assert s.allowed_google_emails == ["user.name@example.com"]
    assert s.admin_google_emails == ["admin.user@example.com"]



def test_settings_custom_paths(tmp_path: Path):
    custom_in = tmp_path / "custom_in"
    custom_out = tmp_path / "custom_out"
    custom_db = tmp_path / "custom.db"
    s = Settings(incoming_dir=custom_in, processed_dir=custom_out, db_path=custom_db)
    assert s.incoming_dir == custom_in
    assert s.processed_dir == custom_out
    assert s.db_path == custom_db


def test_settings_legacy_watch_dir(tmp_path: Path):
    legacy_watch = tmp_path / "legacy_clips"
    s = Settings(watch_dir=legacy_watch)
    assert s.incoming_dir == legacy_watch
    assert s.watch_dir == legacy_watch


def test_settings_save_and_load_effective_json(tmp_path: Path):
    from app.config import load_effective_settings

    config_file = tmp_path / "settings.json"
    s = Settings()
    s.confidence_threshold = 0.65
    s.alert_cooldown_seconds = 120
    s.target_classes = [0, 16]
    s.save_to_json(config_file)

    assert config_file.is_file()

    loaded = load_effective_settings(config_file)
    assert loaded.confidence_threshold == 0.65
    assert loaded.alert_cooldown_seconds == 120
    assert loaded.target_classes == [0, 16]


def test_settings_save_and_load_effective_yaml(tmp_path: Path):
    from app.config import load_effective_settings

    config_file = tmp_path / "settings.yaml"
    s = Settings()
    s.confidence_threshold = 0.55
    s.alert_cooldown_seconds = 45
    s.target_classes = [0, 2, 16]
    s.save_to_yaml(config_file)

    assert config_file.is_file()
    # Check that comments exist in rendered file
    content = config_file.read_text()
    assert "# ── Detection" in content
    assert "# COCO target class IDs" in content

    loaded = load_effective_settings(config_file)
    assert loaded.confidence_threshold == 0.55
    assert loaded.alert_cooldown_seconds == 45
    assert loaded.target_classes == [0, 2, 16]


def test_settings_update_with():
    s = Settings()
    updated = s.update_with({"confidence_threshold": 0.88, "vid_stride": 15})
    assert updated.confidence_threshold == 0.88
    assert updated.vid_stride == 15
    # Original should be unchanged
    assert s.confidence_threshold == 0.45


def test_settings_cameras_per_camera_overrides(tmp_path: Path):
    from app.config import CameraConfig, load_effective_settings

    config_file = tmp_path / "settings.yaml"
    s = Settings()
    s.target_classes = ["person", "car"]
    s.confidence_threshold = 0.45
    s.cameras = [
        CameraConfig(
            name="Frontdoor",
            serial="CAM0200000002",
            enabled=True,
            target_classes=["person", "dog"],
            confidence_threshold=0.55,
            cooldown_seconds=15,
        ),
        CameraConfig(
            name="Unused",
            serial="CAM0300000003",
            enabled=False,
        ),
    ]
    s.save_to_yaml(config_file)

    loaded = load_effective_settings(config_file)
    assert len(loaded.cameras) == 2
    assert loaded.is_camera_enabled("Frontdoor") is True
    assert loaded.is_camera_enabled("Unused") is False
    assert loaded.is_camera_enabled("NonExistent") is True

    # Check target classes resolution
    assert loaded.get_camera_target_classes("Frontdoor") == [0, 16]
    # Unspecified camera uses global
    assert loaded.get_camera_target_classes("Unused") == [0, 2]

    # Check confidence threshold resolution
    assert loaded.get_camera_confidence_threshold("Frontdoor") == 0.55
    assert loaded.get_camera_confidence_threshold("Unused") == 0.45

    # Check mapping
    mapping = loaded.get_camera_mapping()
    assert mapping["CAM0200000002"] == "Frontdoor"
    assert mapping["CAM0300000003"] == "Unused"


def test_https_settings_defaults():
    s = Settings(_env_file=None)
    assert s.domain_name is None
    assert s.acme_email is None
    assert s.https_port == 8443
    assert s.http_port == 8080
    assert s.ssl_cert_path is None
    assert s.ssl_key_path is None


def test_caddyfile_generation_local_ca():
    s = Settings(_env_file=None, https_port=8443, http_port=8080)
    caddyfile = s.generate_caddyfile()
    assert "auto_https disable_redirects" in caddyfile
    assert "default_sni localhost" in caddyfile
    assert ":8443, localhost:8443, 127.0.0.1:8443 {" in caddyfile
    assert "tls internal" in caddyfile
    assert "reverse_proxy sightline-core:8080" in caddyfile
    assert "@blocked path" in caddyfile
    assert "respond @blocked 404" in caddyfile
    assert "http://:8080 {" in caddyfile
    assert "header -Server" in caddyfile
    assert "header_down -Server" in caddyfile
    assert "header_up X-Forwarded-Proto http" in caddyfile


def test_caddyfile_generation_domain_acme():
    s = Settings(
        _env_file=None,
        domain_name="cameras.example.com",
        acme_email="admin@example.com",
        https_port=443,
        http_port=80,
    )
    caddyfile = s.generate_caddyfile()
    assert "auto_https disable_redirects" in caddyfile
    assert "email admin@example.com" in caddyfile
    assert "cameras.example.com {" in caddyfile
    assert "reverse_proxy sightline-core:8080" in caddyfile
    assert "@blocked path" in caddyfile
    assert "respond @blocked 404" in caddyfile
    assert "http://:80 {" in caddyfile
    assert "@public_domain host cameras.example.com" in caddyfile
    assert "redir @public_domain https://{host}{uri}" in caddyfile
    assert "https://localhost:443" in caddyfile
    assert "tls internal" in caddyfile
    assert "header_up X-Forwarded-Proto http" in caddyfile


def test_caddyfile_generation_custom_certs(tmp_path: Path):
    cert = tmp_path / "fullchain.pem"
    key = tmp_path / "privkey.pem"
    s = Settings(
        _env_file=None,
        domain_name="cameras.example.com",
        ssl_cert_path=cert,
        ssl_key_path=key,
        https_port=8443,
    )
    caddyfile = s.generate_caddyfile()
    assert f"tls {cert} {key}" in caddyfile
    assert "cameras.example.com:8443 {" in caddyfile


def test_save_caddyfile(tmp_path: Path):
    caddy_path = tmp_path / "Caddyfile"
    s = Settings(_env_file=None, https_port=8443)
    saved = s.save_caddyfile(caddy_path)
    assert saved == caddy_path
    assert caddy_path.is_file()
    content = caddy_path.read_text()
    assert ":8443 {" in content


def test_apprise_urls_scheme_whitelist_and_rejection():
    import pytest

    valid_urls = [
        "https://webhook.site/test",
        "http://lan-server:8080/hook",
        "ntfys://ntfy.sh/my-topic",
        "ntfy://ntfy.sh/my-topic",
        "pover://AppToken/UserKey",
        "mailto://user:pass@smtp.example.com",
        "discord://webhook_id/webhook_token",
    ]
    s = Settings(_env_file=None, apprise_urls=valid_urls)
    assert s.apprise_urls == valid_urls

    # Local file:// scheme is strictly rejected
    with pytest.raises(ValueError, match="not permitted"):
        Settings(_env_file=None, apprise_urls=["file:///etc/passwd"])

    with pytest.raises(ValueError, match="not permitted"):
        Settings(_env_file=None, apprise_urls=["file:///volume1/sightline/data/test.txt"])

    # Disallowed or invalid scheme is rejected
    with pytest.raises(ValueError, match="not permitted"):
        Settings(_env_file=None, apprise_urls=["customproto://foo"])


def test_caddyfile_injection_prevention():
    import pytest

    # Newline in domain_name rejected
    with pytest.raises(ValueError, match="newlines, spaces, or control characters"):
        Settings(_env_file=None, domain_name="example.com\nheader X-Injected evil")

    # Braces in domain_name rejected
    with pytest.raises(ValueError, match="newlines, spaces, or control characters"):
        Settings(_env_file=None, domain_name="example.com { evil }")

    # Newline in acme_email rejected
    with pytest.raises(ValueError, match="newlines or control characters"):
        Settings(_env_file=None, acme_email="admin@example.com\nheader Evil true")

    # Newline in ssl paths rejected
    with pytest.raises(ValueError, match="newlines or control characters"):
        Settings(_env_file=None, ssl_cert_path=Path("/cert.pem\nevil"))

    # Defensive stripping in generate_caddyfile()
    s = Settings(_env_file=None, domain_name="cameras.example.com", acme_email="admin@example.com")
    caddyfile = s.generate_caddyfile()
    assert "email admin@example.com" in caddyfile
    assert "cameras.example.com {" in caddyfile


def test_camera_config_injection_rejection():
    import pytest
    from app.config import CameraConfig

    # Valid camera config
    cam = CameraConfig(name="Frontdoor", serial="ABC-123_xyz")
    assert cam.name == "Frontdoor"
    assert cam.serial == "ABC-123_xyz"

    # Reject empty name / serial
    with pytest.raises(ValueError, match="cannot be empty"):
        CameraConfig(name="  ", serial="123")
    with pytest.raises(ValueError, match="cannot be empty"):
        CameraConfig(name="Cam", serial="  ")

    # Reject newlines
    with pytest.raises(ValueError, match="must not contain newlines"):
        CameraConfig(name="Cam\nevil: true", serial="123")
    with pytest.raises(ValueError, match="must not contain newlines"):
        CameraConfig(name="Cam", serial="123\r\n")

    # Reject quotes and braces
    with pytest.raises(ValueError, match="must not contain newlines"):
        CameraConfig(name='Cam"injection', serial="123")
    with pytest.raises(ValueError, match="must not contain newlines"):
        CameraConfig(name="Cam", serial="123{evil}")

    # Reject path traversal and slashes
    with pytest.raises(ValueError, match="path traversal"):
        CameraConfig(name="../../etc/passwd", serial="123")
    with pytest.raises(ValueError, match="path traversal"):
        CameraConfig(name="Cam", serial="sub/dir")


def test_yaml_template_escaping(tmp_path: Path):
    from app.config import Settings, CameraConfig
    import yaml

    s = Settings(
        _env_file=None,
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=tmp_path / "models",
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=tmp_path / "settings.yaml",
        domain_name='my.domain.com" # injection',
        cameras=[CameraConfig(name="Driveway", serial="SN12345")],
    )
    rendered = s.render_yaml_template()
    # Ensure it parses as valid YAML
    parsed = yaml.safe_load(rendered)
    assert parsed["domain_name"] == 'my.domain.com" # injection'
    assert parsed["cameras"][0]["name"] == "Driveway"
    assert parsed["cameras"][0]["serial"] == "SN12345"


def test_caddyfile_management_routes_and_http_headers():
    s = Settings(
        _env_file=None,
        domain_name="sightline.example.com",
        https_port=4210,
        http_port=4280,
    )
    caddyfile = s.generate_caddyfile()

    # Management routes blocked via stealth 404
    assert "/api/v1/clips" in caddyfile
    assert "/api/v1/devices*" in caddyfile
    assert "/api/v1/preferences*" in caddyfile
    assert "/api/v1/settings*" in caddyfile
    assert "/api/v1/clips/process" in caddyfile
    # Notice /api/v1/clips/scan is intentionally NOT in blocked matcher
    assert "/api/v1/clips/scan" not in caddyfile

    # HTTP Port 4280 contains security headers and body limit
    http_section = caddyfile[caddyfile.find("http://:4280 {"):]
    assert "Strict-Transport-Security" in http_section
    assert "X-Content-Type-Options" in http_section
    assert "X-Frame-Options" in http_section
    assert "request_body {" in http_section
    assert "max_size 10MB" in http_section


def test_caddyfile_lan_proxy_and_hosts():
    s = Settings(
        _env_file=None,
        domain_name="sightline.example.com",
        https_port=4210,
        http_port=4280,
        lan_hosts=["192.168.1.100", "nas.local"],
    )
    caddyfile = s.generate_caddyfile()
    assert "https://192.168.1.100:4210" in caddyfile
    assert "https://nas.local:4210" in caddyfile
    assert "https://localhost:4210" in caddyfile
    assert "https://127.0.0.1:4210" in caddyfile

    http_section = caddyfile[caddyfile.find("http://:4280 {"):]
    assert "@public_domain host sightline.example.com" in http_section
    assert "@not_lan not remote_ip" in http_section
    assert "reverse_proxy" in http_section
    assert "header_up X-Sightline-Access lan" in http_section


def test_caddyfile_phase9_security_hardening():
    s = Settings(
        _env_file=None,
        domain_name="sightline.example.com",
        https_port=4210,
        http_port=4280,
        lan_hosts=["192.168.1.100"],
    )
    caddyfile = s.generate_caddyfile()

    # 1. Global server timeouts for Slowloris mitigation
    assert "servers {" in caddyfile
    assert "timeouts {" in caddyfile
    assert "read_body 15s" in caddyfile
    assert "read_header 10s" in caddyfile

    # 2. HTTP/3 Alt-Svc suppression
    assert "header -Alt-Svc" in caddyfile

    # 3. Modern CSP with frame-ancestors 'self' and CORP
    assert "frame-ancestors 'self'" in caddyfile
    assert "object-src 'none'" in caddyfile
    assert "base-uri 'self'" in caddyfile
    assert 'header Cross-Origin-Resource-Policy "same-origin"' in caddyfile

    # 4. Stealth 404 for WAN on LAN HTTPS block
    lan_block = caddyfile[caddyfile.find("HTTPS for local LAN"):caddyfile.find("http://:4280")]
    assert "@not_lan not remote_ip" in lan_block
    assert "respond @not_lan 404" in lan_block

    # 5. Header -Server, -Via, and -Alt-Svc inside handle_path /static/*
    assert "handle_path /static/* {\n        header -Server\n        header -Via\n        header -Alt-Svc" in caddyfile


def test_class_confidence_thresholds_normalization_and_merge(tmp_path: Path):
    from app.config import CameraConfig, Settings, load_effective_settings

    s = Settings(
        confidence_threshold=0.45,
        class_confidence_thresholds={"person": 50, "16": 0.55},
        cameras=[
            CameraConfig(
                name="Frontdoor",
                serial="CAM0100000001",
                confidence_threshold=0.40,
                class_confidence_thresholds={"person": 60, "car": 0.70},
            ),
            CameraConfig(
                name="Backyard",
                serial="CAM0400000004",
            ),
        ],
    )

    # 1. Global normalization
    assert s.class_confidence_thresholds["person"] == 0.50
    assert s.class_confidence_thresholds["dog"] == 0.55

    # 2. Frontdoor merged thresholds: person overridden to 0.60, dog inherited from global 0.55, car added
    front_thresholds = s.get_camera_class_confidence_thresholds("Frontdoor")
    assert front_thresholds[0] == 0.60
    assert front_thresholds[16] == 0.55
    assert front_thresholds[2] == 0.70

    # Fallback threshold for unlisted classes on Frontdoor
    assert s.get_camera_confidence_threshold("Frontdoor") == 0.40

    # 3. Backyard merged thresholds: inherits global person and dog
    back_thresholds = s.get_camera_class_confidence_thresholds("Backyard")
    assert back_thresholds[0] == 0.50
    assert back_thresholds[16] == 0.55
    assert 2 not in back_thresholds
    assert s.get_camera_confidence_threshold("Backyard") == 0.45

    # 4. YAML persistence and reload
    yaml_file = tmp_path / "settings_class_conf.yaml"
    s.save_to_yaml(yaml_file)
    assert "class_confidence_thresholds:" in yaml_file.read_text()

    loaded = load_effective_settings(yaml_file)
    assert loaded.class_confidence_thresholds.get("person") == 0.50
    assert loaded.class_confidence_thresholds.get("dog") == 0.55
    loaded_front = loaded.get_camera_class_confidence_thresholds("Frontdoor")
    assert loaded_front[0] == 0.60
    assert loaded_front[2] == 0.70


def test_caddyfile_tunnel_mode():
    s = Settings(
        _env_file=None,
        domain_name="sightline.example.com",
        https_port=4210,
        tunnel_mode=True,
    )
    caddyfile = s.generate_caddyfile()
    assert "sightline.example.com, sightline.example.com:4210, caddy:4210, sightline-caddy:4210 {" in caddyfile
    assert "tls internal" in caddyfile
    assert "header_up X-Real-IP {http.request.header.Cf-Connecting-Ip}" in caddyfile






