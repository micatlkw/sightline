import asyncio
import json
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

from app.config import Settings
from app.core.settings_watcher import SettingsWatcher
from app.main import create_app


@pytest.mark.asyncio
async def test_get_and_put_settings_api(tmp_path: Path):
    config_file = tmp_path / "settings.yaml"
    settings = Settings(
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=tmp_path / "models",
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=config_file,
    )
    settings.save_to_yaml()

    app = create_app(settings=settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. GET /api/v1/settings
        resp = await ac.get("/api/v1/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "settings" in data
        assert data["settings"]["confidence_threshold"] == 0.45

        # 2. PUT /api/v1/settings
        update_payload = {
            "confidence_threshold": 0.75,
            "alert_cooldown_seconds": 30,
            "target_classes": ["person", "car"],
            "domain_name": "cam.example.com",
            "https_port": 8443,
            "cameras": [
                {"name": "Frontdoor", "serial": "CAM0200000002", "enabled": True, "target_classes": ["person"]},
                {"name": "Unused", "serial": "CAM0300000003", "enabled": False},
            ],
        }
        resp = await ac.put("/api/v1/settings", json=update_payload)
        assert resp.status_code == 200
        res_data = resp.json()
        assert res_data["status"] == "updated"
        assert res_data["settings"]["confidence_threshold"] == 0.75
        assert res_data["settings"]["alert_cooldown_seconds"] == 30
        assert res_data["settings"]["target_classes"] == ["person", "car"]
        assert res_data["settings"]["domain_name"] == "cam.example.com"
        assert res_data["settings"]["https_port"] == 8443
        assert len(res_data["settings"]["cameras"]) == 2

        # Verify disk file updated in YAML format with comments
        assert config_file.is_file()
        import yaml
        file_yaml = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert file_yaml["confidence_threshold"] == 0.75
        assert file_yaml["alert_cooldown_seconds"] == 30
        assert file_yaml["domain_name"] == "cam.example.com"
        assert file_yaml["https_port"] == 8443
        assert len(file_yaml["cameras"]) == 2
        assert file_yaml["cameras"][0]["name"] == "Frontdoor"
        assert "# ── Detection" in config_file.read_text(encoding="utf-8")
        assert "# ── HTTPS & Reverse Proxy" in config_file.read_text(encoding="utf-8")

        # Verify Caddyfile auto-generated
        caddy_file = config_file.parent / "Caddyfile"
        assert caddy_file.is_file()
        assert "cam.example.com:8443" in caddy_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_settings_watcher_reload_yaml(tmp_path: Path):
    import yaml
    config_file = tmp_path / "settings.yaml"
    s = Settings(
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=tmp_path / "models",
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=config_file,
    )
    s.confidence_threshold = 0.5
    s.save_to_yaml()

    reloaded_settings = []

    def on_reloaded(new_s: Settings):
        reloaded_settings.append(new_s)

    watcher = SettingsWatcher(
        config_path=config_file,
        on_reloaded=on_reloaded,
        debounce_seconds=0.1,
    )
    await watcher.start()

    try:
        # Modify file on disk in YAML
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        data["confidence_threshold"] = 0.85
        config_file.write_text(yaml.dump(data), encoding="utf-8")

        # Explicitly reload
        reloaded = await watcher.reload()
        assert reloaded is not None
        assert reloaded.confidence_threshold == 0.85
        assert len(reloaded_settings) == 1
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_wan_settings_restrictions(tmp_path: Path):
    from app.auth.google_sso import User, create_session_token, get_session_secret
    from app.config import CameraConfig
    config_file = tmp_path / "settings.yaml"
    settings = Settings(
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=tmp_path / "models",
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=config_file,
        allowed_google_emails=["viewer@example.com", "admin@example.com"],
        admin_google_emails=["admin@example.com"],
        allow_lan_auth_bypass=False,
    )
    settings.cameras = [
        CameraConfig(name="Cam1", serial="CAM1_SERIAL", enabled=True),
        CameraConfig(name="Cam2", serial="CAM2_SERIAL", enabled=True),
    ]
    settings.save_to_yaml()

    app = create_app(settings=settings)
    secret = get_session_secret(settings)
    viewer_token = create_session_token(User(email="viewer@example.com"), secret)
    admin_token = create_session_token(User(email="admin@example.com"), secret)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # WAN request with non-admin user -> 403
        wan_headers_viewer = {"Authorization": f"Bearer {viewer_token}", "X-Sightline-Access": "wan"}
        resp = await ac.put("/api/v1/settings", json={"confidence_threshold": 0.8}, headers=wan_headers_viewer)
        assert resp.status_code == 403

        # WAN request with admin user updating non-camera fields -> 403
        wan_headers_admin = {"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"}
        resp = await ac.put("/api/v1/settings", json={"confidence_threshold": 0.8}, headers=wan_headers_admin)
        assert resp.status_code == 403

        # WAN request with admin user deleting a camera -> 403
        del_payload = {
            "cameras": [
                {"name": "Cam1", "serial": "CAM1_SERIAL", "enabled": False}
            ]
        }
        resp = await ac.put("/api/v1/settings", json=del_payload, headers=wan_headers_admin)
        assert resp.status_code == 403

        # WAN request with admin updating camera settings without deleting -> 200
        ok_payload = {
            "cameras": [
                {"name": "Cam1", "serial": "CAM1_SERIAL", "enabled": False},
                {"name": "Cam2", "serial": "CAM2_SERIAL", "enabled": True},
            ]
        }
        resp = await ac.put("/api/v1/settings", json=ok_payload, headers=wan_headers_admin)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_notification_test_endpoint_security(tmp_path: Path):
    from app.auth.google_sso import User, create_session_token, get_session_secret
    config_file = tmp_path / "settings.yaml"
    settings = Settings(
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=tmp_path / "models",
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=config_file,
        allowed_google_emails=["viewer@example.com", "admin@example.com"],
        admin_google_emails=["admin@example.com"],
        allow_lan_auth_bypass=False,
    )
    settings.save_to_yaml()

    app = create_app(settings=settings)
    secret = get_session_secret(settings)
    viewer_token = create_session_token(User(email="viewer@example.com"), secret)
    admin_token = create_session_token(User(email="admin@example.com"), secret)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Unauthenticated -> 401
        res = await ac.post("/api/v1/notifications/test")
        assert res.status_code == 401

        # 2. Non-admin on WAN -> 403
        res = await ac.post(
            "/api/v1/notifications/test",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Sightline-Access": "wan"},
        )
        assert res.status_code == 403

        # 3. Admin on WAN -> 200
        res = await ac.post(
            "/api/v1/notifications/test",
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
        )
        assert res.status_code == 200

        # 4. Immediate second call -> 429 cooldown
        res_cooldown = await ac.post(
            "/api/v1/notifications/test",
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
        )
        assert res_cooldown.status_code == 429


@pytest.mark.asyncio
async def test_get_settings_wan_rbac(tmp_path: Path):
    from app.auth.google_sso import User, create_session_token, get_session_secret

    config_file = tmp_path / "settings.yaml"
    settings = Settings(
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=tmp_path / "models",
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=config_file,
        allowed_google_emails=["viewer@example.com", "admin@example.com"],
        admin_google_emails=["admin@example.com"],
        allow_lan_auth_bypass=False,
    )
    settings.save_to_yaml()

    app = create_app(settings=settings)
    secret = get_session_secret(settings)
    viewer_token = create_session_token(User(email="viewer@example.com"), secret)
    admin_token = create_session_token(User(email="admin@example.com"), secret)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Non-admin on WAN -> 403 Forbidden
        res_viewer_wan = await ac.get(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Sightline-Access": "wan"},
        )
        assert res_viewer_wan.status_code == 403
        assert "Admin privileges required to view settings over WAN" in res_viewer_wan.json()["detail"]

        # 2. Admin on WAN -> 200 OK
        res_admin_wan = await ac.get(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
        )
        assert res_admin_wan.status_code == 200
        assert "settings" in res_admin_wan.json()

        # 3. Non-admin on LAN -> 200 OK
        res_viewer_lan = await ac.get(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Sightline-Access": "lan"},
        )
        assert res_viewer_lan.status_code == 200


@pytest.mark.asyncio
async def test_update_settings_user_management(tmp_path: Path):
    from app.auth.google_sso import User, create_session_token, get_session_secret

    config_file = tmp_path / "settings.yaml"
    settings = Settings(
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=tmp_path / "models",
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=config_file,
        allowed_google_emails=["admin@example.com"],
        admin_google_emails=["admin@example.com"],
        allow_lan_auth_bypass=False,
    )
    settings.save_to_yaml()

    app = create_app(settings=settings)
    secret = get_session_secret(settings)
    admin_token = create_session_token(User(email="admin@example.com"), secret)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Admin adds a viewer and admin on WAN -> 200 OK
        res = await ac.put(
            "/api/v1/settings",
            json={
                "allowed_google_emails": ["admin@example.com", "viewer@example.com"],
                "admin_google_emails": ["admin@example.com"],
            },
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
        )
        assert res.status_code == 200
        saved = res.json()["settings"]
        assert "viewer@example.com" in saved["allowed_google_emails"]
        assert saved["admin_google_emails"] == ["admin@example.com"]

        # 2. Admin email automatically included in allowed_google_emails even if omitted
        res_auto = await ac.put(
            "/api/v1/settings",
            json={
                "allowed_google_emails": ["viewer@example.com"],
                "admin_google_emails": ["admin@example.com", "super@example.com"],
            },
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
        )
        assert res_auto.status_code == 200
        saved_auto = res_auto.json()["settings"]
        assert "super@example.com" in saved_auto["allowed_google_emails"]
        assert "admin@example.com" in saved_auto["allowed_google_emails"]

        # 3. Attempting to save zero admins when allowed users exist -> 400 Bad Request
        res_zero = await ac.put(
            "/api/v1/settings",
            json={
                "allowed_google_emails": ["viewer@example.com"],
                "admin_google_emails": [],
            },
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
        )
        assert res_zero.status_code == 400
        assert "At least one administrator account must be designated" in res_zero.json()["detail"]

        # 4. Admin attempting to demote/remove their own email over WAN -> 400 Bad Request (Self-lockout guard)
        res_self = await ac.put(
            "/api/v1/settings",
            json={
                "allowed_google_emails": ["viewer@example.com", "super@example.com"],
                "admin_google_emails": ["super@example.com"],
            },
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
        )
        assert res_self.status_code == 400
        assert "Cannot remove or demote your own administrator account" in res_self.json()["detail"]


@pytest.mark.asyncio
async def test_settings_api_camera_validation(tmp_path: Path):
    config_file = tmp_path / "settings.yaml"
    settings = Settings(
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=tmp_path / "models",
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=config_file,
    )
    settings.save_to_yaml()

    app = create_app(settings=settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Invalid camera name with injection / path traversal -> 422 Unprocessable Entity
        res_invalid = await ac.put(
            "/api/v1/settings",
            json={
                "cameras": [
                    {"name": "../../etc/passwd", "serial": "SN123"},
                ],
            },
        )
        assert res_invalid.status_code == 422


@pytest.mark.asyncio
async def test_cached_models_endpoint_and_wan_activation(tmp_path: Path):
    from app.auth.google_sso import User, create_session_token, get_session_secret

    config_file = tmp_path / "settings.yaml"
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "yolo11n.pt").write_bytes(b"dummy-model-n" * 1000)
    (models_dir / "yolo11s.pt").write_bytes(b"dummy-model-s" * 2000)
    (models_dir / "custom_detector.onnx").write_bytes(b"dummy-model-onnx" * 500)

    settings = Settings(
        incoming_dir=tmp_path / "in",
        processed_dir=tmp_path / "out",
        db_path=tmp_path / "test.db",
        models_dir=models_dir,
        thumbnails_dir=tmp_path / "thumbs",
        settings_config_path=config_file,
        yolo_model="yolo11s.pt",
        allowed_google_emails=["viewer@example.com", "admin@example.com"],
        admin_google_emails=["admin@example.com"],
        allow_lan_auth_bypass=False,
    )
    settings.save_to_yaml()

    app = create_app(settings=settings)
    secret = get_session_secret(settings)
    admin_token = create_session_token(User(email="admin@example.com"), secret)
    viewer_token = create_session_token(User(email="viewer@example.com"), secret)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. GET /api/v1/settings/models
        res = await ac.get(
            "/api/v1/settings/models",
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
        )
        assert res.status_code == 200
        data = res.json()
        assert "models" in data
        assert data["current_model"] == "yolo11s.pt"
        model_names = [m["name"] for m in data["models"]]
        assert "yolo11s.pt" in model_names
        assert "yolo11n.pt" in model_names
        assert "custom_detector.onnx" in model_names

        # Check active flag
        active_entry = next(m for m in data["models"] if m["name"] == "yolo11s.pt")
        assert active_entry["active"] is True

        # 2. Admin activates a cached model over WAN -> 200 OK
        res_wan_ok = await ac.put(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
            json={"yolo_model": "yolo11n.pt"},
        )
        assert res_wan_ok.status_code == 200
        assert res_wan_ok.json()["settings"]["yolo_model"] == "yolo11n.pt"

        # 3. Admin attempts to activate an uncached model over WAN -> 400 Bad Request
        res_wan_fail = await ac.put(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
            json={"yolo_model": "non_existent_yolo.pt"},
        )
        assert res_wan_fail.status_code == 400
        assert "not cached locally" in res_wan_fail.json()["detail"]

        # 4. Non-admin attempts to change model over WAN -> 403 Forbidden
        res_viewer_fail = await ac.put(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {viewer_token}", "X-Sightline-Access": "wan"},
            json={"yolo_model": "yolo11s.pt"},
        )
        assert res_viewer_fail.status_code == 403

        # 5. Admin can toggle camera enabled/disabled over WAN -> 200 OK
        res_cam_toggle = await ac.put(
            "/api/v1/settings",
            headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
            json={
                "cameras": [
                    {"name": "Frontdoor", "serial": "SN123", "enabled": False},
                ]
            },
        )
        assert res_cam_toggle.status_code == 200
        assert res_cam_toggle.json()["settings"]["cameras"][0]["enabled"] is False


