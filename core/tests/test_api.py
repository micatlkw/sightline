import pytest
from httpx import AsyncClient, ASGITransport

from app.config import Settings
from app.core.event_bus import EventBus
from app.database import Database
from app.main import create_app
from app.models.event import DetectionItem


@pytest.fixture
def app_instance(test_settings: Settings):
    return create_app(test_settings)


@pytest.mark.asyncio
async def test_health_endpoint(app_instance):
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as client:
        # Trigger lifespan
        async with app_instance.router.lifespan_context(app_instance):
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "watcher" in data
            assert "pipeline" in data
            assert "detection" in data


@pytest.mark.asyncio
async def test_events_crud_and_thumbnail(app_instance, test_settings: Settings):
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as client:
        async with app_instance.router.lifespan_context(app_instance):
            db: Database = app_instance.state.db

            # Initially empty
            resp = await client.get("/events")
            assert resp.status_code == 200
            assert resp.json()["events"] == []

            # Save test event
            thumb_file = test_settings.thumbnails_dir / "test_thumb.jpg"
            thumb_file.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01")  # minimal jpeg header

            clip_file = test_settings.watch_dir / "frontdoor" / "clip.mp4"
            clip_file.parent.mkdir(parents=True, exist_ok=True)
            clip_file.write_bytes(b"dummy")

            evt = await db.save_event(
                clip_file,
                [DetectionItem(0, "person", 0.95, 1.0, [0.1, 0.1, 0.5, 0.5], str(thumb_file))]
            )

            # Get list
            resp = await client.get("/events")
            assert resp.status_code == 200
            events = resp.json()["events"]
            assert len(events) == 1
            assert events[0]["camera_name"] == "frontdoor"

            # Filter by camera
            resp_fd = await client.get("/events?camera=frontdoor")
            assert len(resp_fd.json()["events"]) == 1

            resp_other = await client.get("/events?camera=nonexistent")
            assert len(resp_other.json()["events"]) == 0

            # Filter by class
            resp_person = await client.get("/events?cls=person")
            assert len(resp_person.json()["events"]) == 1

            resp_bear = await client.get("/events?cls=bear")
            assert len(resp_bear.json()["events"]) == 0

            # Get single event
            resp_single = await client.get(f"/events/{evt.id}")
            assert resp_single.status_code == 200
            assert resp_single.json()["id"] == evt.id

            # Get non-existent event
            resp_404 = await client.get("/events/999999")
            assert resp_404.status_code == 404

            # Get thumbnail (JPEG)
            resp_thumb = await client.get(f"/events/{evt.id}/thumbnail")
            assert resp_thumb.status_code == 200
            assert resp_thumb.headers["content-type"] == "image/jpeg"

            # Test GIF thumbnail
            gif_thumb = test_settings.thumbnails_dir / "test_thumb.gif"
            gif_thumb.write_bytes(b"GIF89a\x01\x00\x01\x00")
            evt_gif = await db.save_event(
                clip_file,
                [DetectionItem(0, "person", 0.95, 1.0, [0.1, 0.1, 0.5, 0.5], str(gif_thumb))]
            )
            resp_gif = await client.get(f"/events/{evt_gif.id}/thumbnail")
            assert resp_gif.status_code == 200
            assert resp_gif.headers["content-type"] == "image/gif"



@pytest.mark.asyncio
async def test_clips_endpoints(app_instance, test_settings: Settings):
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as client:
        async with app_instance.router.lifespan_context(app_instance):
            # List clips (initially empty)
            resp = await client.get("/clips")
            assert resp.status_code == 200
            assert resp.json()["clips"] == []

            # POST /clips/process with non-existent file -> 404
            resp_err = await client.post("/clips/process", json={"path": "/nonexistent.mp4"})
            assert resp_err.status_code == 404

            # POST /clips/process with non-mp4 file -> 400
            bad_file = test_settings.watch_dir / "test.txt"
            bad_file.write_text("not a video")
            resp_bad = await client.post("/clips/process", json={"path": str(bad_file)})
            assert resp_bad.status_code == 400

            # POST /clips/process with valid mp4 file -> 202
            good_file = test_settings.watch_dir / "manual_clip.mp4"
            good_file.write_bytes(b"dummy video")
            resp_ok = await client.post("/clips/process", json={"path": str(good_file)})
            assert resp_ok.status_code == 202
            assert resp_ok.json()["status"] == "queued"

            # POST /clips/scan -> 200
            scan_file = test_settings.watch_dir / "scanned_clip.mp4"
            scan_file.write_bytes(b"scan video")
            resp_scan = await client.post("/clips/scan")
            assert resp_scan.status_code == 200
            scan_data = resp_scan.json()
            assert scan_data["status"] == "ok"
            assert "queued_clips" not in scan_data  # Sanitized to prevent server path disclosure
            assert scan_data["queued_count"] >= 1


def test_websocket_events(app_instance):
    from starlette.testclient import TestClient

    with TestClient(app_instance) as client:
        with client.websocket_connect("/ws/events") as ws:
            # Publish event onto app.state.bus
            bus: EventBus = app_instance.state.bus
            test_event = {
                "id": 101,
                "clip_path": "/data/clips/frontdoor.mp4",
                "camera_name": "frontdoor",
                "detected_at": "2026-08-23T10:00:00Z",
                "thumbnail_url": "/events/101/thumbnail",
                "objects": [{"class": "person", "confidence": 0.95}],
            }
            bus.publish("detection", test_event)

            # Receive on websocket
            data = ws.receive_json()
            assert data["id"] == 101
            assert data["camera_name"] == "frontdoor"
            assert data["objects"][0]["class"] == "person"


@pytest.mark.asyncio
async def test_process_clip_boundary_containment(app_instance, test_settings: Settings, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as client:
        async with app_instance.router.lifespan_context(app_instance):
            # 1. Path outside incoming_dir -> 400 Bad Request
            outside_file = tmp_path / "outside.mp4"
            outside_file.write_bytes(b"dummy")
            res_outside = await client.post("/clips/process", json={"path": str(outside_file)})
            assert res_outside.status_code == 400
            assert "must be located within incoming directory" in res_outside.json()["detail"]

            # 2. Path inside incoming_dir -> 202 Accepted
            inside_file = test_settings.incoming_dir / "valid_clip.mp4"
            inside_file.write_bytes(b"dummy")
            res_inside = await client.post("/clips/process", json={"path": str(inside_file)})
            assert res_inside.status_code == 202


@pytest.mark.asyncio
async def test_event_media_containment(app_instance, test_settings: Settings, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as client:
        async with app_instance.router.lifespan_context(app_instance):
            db: Database = app_instance.state.db

            # Save event with clip_path outside allowed directories
            outside_file = tmp_path / "secret.txt"
            outside_file.write_text("sensitive data")

            # Insert directly into events table
            async with db._conn.execute(
                "INSERT INTO events (clip_path, camera_name, detected_at, objects, thumbnail) VALUES (?, ?, ?, ?, ?)",
                (str(outside_file), "test_cam", "2026-09-03T10:00:00Z", "[]", str(outside_file)),
            ) as cur:
                bad_event_id = cur.lastrowid
            await db._conn.commit()

            # Calling /video or /thumbnail must NOT serve the outside file
            res_vid = await client.get(f"/events/{bad_event_id}/video")
            assert res_vid.status_code == 404

            res_thumb = await client.get(f"/events/{bad_event_id}/thumbnail")
            assert res_thumb.status_code == 404


@pytest.mark.asyncio
async def test_rescan_admin_and_cooldown(tmp_path: Path):
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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            # 1. Non-admin on WAN -> 403 Forbidden
            res_viewer = await client.post(
                "/api/v1/events/rescan",
                headers={"Authorization": f"Bearer {viewer_token}", "X-Sightline-Access": "wan"},
            )
            assert res_viewer.status_code == 403
            assert "Admin privileges required" in res_viewer.json()["detail"]

            # 2. Admin on WAN -> 200 OK
            res_admin = await client.post(
                "/api/v1/events/rescan",
                headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
            )
            assert res_admin.status_code == 200
            assert "recovered" in res_admin.json()
            assert "pruned" in res_admin.json()

            # 3. Immediate second call -> 429 Cooldown
            res_repeat = await client.post(
                "/api/v1/events/rescan",
                headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
            )
            assert res_repeat.status_code == 429
            assert "Rescan cooldown active" in res_repeat.json()["detail"]


@pytest.mark.asyncio
async def test_health_sanitization(app_instance):
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_instance.router.lifespan_context(app_instance):
            res = await client.get("/health")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "ok"
            watcher = data["watcher"]
            assert watcher["incoming_configured"] is True
            assert watcher["processed_configured"] is True
            # Verify no filesystem paths are leaked
            data_str = json.dumps(data)
            assert "/volume1" not in data_str
            assert "incoming_dir" not in watcher
            assert "processed_dir" not in watcher


def test_disposition_filename_sanitization():
    from app.api.routes.events import sanitize_disposition_filename

    assert sanitize_disposition_filename("normal_video.mp4") == "normal_video.mp4"
    # Strips quotes, newlines, carriage returns, semicolons, backslashes
    assert sanitize_disposition_filename('evil"\r\nfilename;injection.mp4') == "evil___filename_injection.mp4"
    # Strips path traversal slashes
    assert sanitize_disposition_filename("../../etc/passwd") == ".._.._etc_passwd"
    # Fallback on empty or whitespace string
    assert sanitize_disposition_filename("") == "download"
    assert sanitize_disposition_filename("   \r\n   ") == "download"


@pytest.mark.asyncio
async def test_clips_scan_rbac_and_cooldown(tmp_path: Path):
    from app.auth.google_sso import User, create_session_token, get_session_secret
    import app.api.routes.clips as clips_module

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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            # 1. Non-admin on WAN -> 403 Forbidden
            res_viewer = await client.post(
                "/api/v1/clips/scan",
                headers={"Authorization": f"Bearer {viewer_token}", "X-Sightline-Access": "wan"},
            )
            assert res_viewer.status_code == 403
            assert "Admin privileges required" in res_viewer.json()["detail"]

            # Reset cooldown timestamp
            clips_module._LAST_SCAN_TS = 0.0

            # 2. Admin on WAN -> 200 OK
            res_admin = await client.post(
                "/api/v1/clips/scan",
                headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
            )
            assert res_admin.status_code == 200
            data = res_admin.json()
            assert data["status"] == "ok"
            assert "queued_clips" not in data
            assert "queued_count" in data

            # 3. Immediate repeat -> 429 Cooldown
            res_repeat = await client.post(
                "/api/v1/clips/scan",
                headers={"Authorization": f"Bearer {admin_token}", "X-Sightline-Access": "wan"},
            )
            assert res_repeat.status_code == 429
            assert "Rescan cooldown active" in res_repeat.json()["detail"]
            assert "Retry-After" in res_repeat.headers


def test_event_record_to_dict_sanitization():
    from app.models.event import EventRecord

    rec = EventRecord(
        id=42,
        clip_path="/volume1/sightline/data/processed/2026-09-04/Frontdoor/frontdoor-12345.mp4",
        camera_name="Frontdoor",
        detected_at="2026-09-04T12:00:00Z",
        objects=[{"class": "person", "confidence": 0.95}],
        thumbnail="/volume1/sightline/data/thumbnails/2026-09-04/Frontdoor/frontdoor-12345.jpg",
    )
    d = rec.to_dict()
    # Ensure raw directory paths are completely sanitized
    assert d["clip_path"] == "frontdoor-12345.mp4"
    assert d["clip_filename"] == "frontdoor-12345.mp4"
    assert d["video_url"] == "/events/42/video"
    assert "/volume1" not in str(d["clip_path"])
    assert "/data" not in str(d["clip_path"])


def test_device_register_request_validation():
    from app.api.routes.devices import DeviceRegisterRequest
    import pytest

    # Valid
    req = DeviceRegisterRequest(device_id="dev-123", fcm_token="token-abc", device_name="Living Room")
    assert req.device_id == "dev-123"

    # Empty device_id or fcm_token
    with pytest.raises(ValueError, match="device_id cannot be empty"):
        DeviceRegisterRequest(device_id="   ", fcm_token="tok")
    with pytest.raises(ValueError, match="fcm_token cannot be empty"):
        DeviceRegisterRequest(device_id="dev-1", fcm_token="   ")

    # Newlines / control characters
    with pytest.raises(ValueError, match="control characters"):
        DeviceRegisterRequest(device_id="dev\ninjection", fcm_token="tok")
    with pytest.raises(ValueError, match="control characters"):
        DeviceRegisterRequest(device_id="dev", fcm_token="tok\r\n")
    with pytest.raises(ValueError, match="control characters"):
        DeviceRegisterRequest(device_id="dev", fcm_token="tok", device_name="Name\nBad")


def test_preferences_request_validation():
    from app.api.routes.preferences import CameraPreferenceRequest
    from datetime import datetime, timezone, timedelta
    import pytest

    # Valid
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    req = CameraPreferenceRequest(camera_name="Frontdoor", enabled=True, mute_until=future_iso)
    assert req.camera_name == "Frontdoor"
    assert req.mute_until is not None

    # Empty camera name
    with pytest.raises(ValueError, match="camera_name cannot be empty"):
        CameraPreferenceRequest(camera_name="   ")

    # Invalid ISO timestamp
    with pytest.raises(ValueError, match="valid ISO-8601"):
        CameraPreferenceRequest(camera_name="Frontdoor", mute_until="tomorrow")

    # Past timestamp
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="must be in the future"):
        CameraPreferenceRequest(camera_name="Frontdoor", mute_until=past_iso)

    # > 30 days timestamp
    too_far_iso = (datetime.now(timezone.utc) + timedelta(days=35)).isoformat()
    with pytest.raises(ValueError, match="cannot exceed 30 days"):
        CameraPreferenceRequest(camera_name="Frontdoor", mute_until=too_far_iso)


@pytest.mark.asyncio
async def test_delete_single_and_batch_events_api(app_instance, test_settings: Settings):
    from app.auth.google_sso import User, create_session_token, get_session_secret

    test_settings.allowed_google_emails = ["viewer@example.com", "admin@example.com"]
    test_settings.admin_google_emails = ["admin@example.com"]
    test_settings.allow_lan_auth_bypass = False

    secret = get_session_secret(test_settings)
    admin_token = create_session_token(User(email="admin@example.com"), secret)
    viewer_token = create_session_token(User(email="viewer@example.com"), secret)

    async with AsyncClient(transport=ASGITransport(app=app_instance), base_url="http://test") as client:
        async with app_instance.router.lifespan_context(app_instance):
            db: Database = app_instance.state.db

            # Create test media files
            clip1 = test_settings.processed_dir / "clip1.mp4"
            clip1.write_bytes(b"mp4-content-1")
            thumb1 = test_settings.thumbnails_dir / "clip1.jpg"
            thumb1.write_bytes(b"thumb-content-1")
            gif1 = test_settings.thumbnails_dir / "clip1-1person.gif"
            gif1.write_bytes(b"gif-content-1")

            evt1 = await db.save_event(
                clip1,
                [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1], str(thumb1))],
            )

            clip2 = test_settings.processed_dir / "clip2.mp4"
            clip2.write_bytes(b"mp4-content-2")
            thumb2 = test_settings.thumbnails_dir / "clip2.jpg"
            thumb2.write_bytes(b"thumb-content-2")

            evt2 = await db.save_event(
                clip2,
                [DetectionItem(0, "person", 0.95, 1.0, [0, 0, 1, 1], str(thumb2))],
            )

            # 1. Non-admin cannot delete single event -> 403 Forbidden
            res_no_admin = await client.delete(
                f"/api/v1/events/{evt1.id}",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            assert res_no_admin.status_code == 403

            # 2. Admin deletes single event -> 200 OK
            res_del_ok = await client.delete(
                f"/api/v1/events/{evt1.id}",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert res_del_ok.status_code == 200
            assert res_del_ok.json()["status"] == "deleted"
            assert res_del_ok.json()["id"] == evt1.id

            # Verify files unlinked from disk
            assert not clip1.exists()
            assert not thumb1.exists()
            assert not gif1.exists()

            # Verify DB record gone
            assert await db.get_event(evt1.id) is None

            # 3. Deleting non-existent event -> 404 (DELETE & POST alias)
            res_404 = await client.delete(
                f"/api/v1/events/{evt1.id}",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert res_404.status_code == 404

            res_404_post = await client.post(
                f"/api/v1/events/{evt1.id}/delete",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert res_404_post.status_code == 404

            # 4. Batch delete non-admin -> 403 Forbidden
            res_batch_no_admin = await client.post(
                "/api/v1/events/batch-delete",
                headers={"Authorization": f"Bearer {viewer_token}"},
                json={"event_ids": [evt2.id]},
            )
            assert res_batch_no_admin.status_code == 403

            # 5. Batch delete admin -> 200 OK
            res_batch_ok = await client.post(
                "/api/v1/events/batch-delete",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"event_ids": [evt2.id, 99999]},
            )
            assert res_batch_ok.status_code == 200
            data = res_batch_ok.json()
            assert data["status"] == "deleted"
            assert evt2.id in data["deleted_ids"]
            assert 99999 in data["failed_ids"]

            assert not clip2.exists()
            assert not thumb2.exists()
            assert await db.get_event(evt2.id) is None


