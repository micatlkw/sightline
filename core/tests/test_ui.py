import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.config import Settings
from app.database import Database
from app.models.event import DetectionItem
from app.main import create_app


@pytest.fixture
def test_env(tmp_path: Path):
    db_file = tmp_path / "test.db"
    incoming = tmp_path / "incoming"
    processed = tmp_path / "processed"
    thumbnails = tmp_path / "thumbnails"
    models = tmp_path / "models"
    config_yaml = tmp_path / "settings.yaml"

    for d in (incoming, processed, thumbnails, models):
        d.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        db_path=db_file,
        incoming_dir=incoming,
        processed_dir=processed,
        thumbnails_dir=thumbnails,
        models_dir=models,
        settings_config_path=config_yaml,
        allowed_google_emails=[],
        ui_port=8080,
    )
    return settings


@pytest.mark.asyncio
async def test_ui_index_endpoint(test_env: Settings):
    app = create_app(test_env)
    with TestClient(app) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "Sightline" in res.text
        assert "Events" in res.text
        assert "Settings" in res.text


@pytest.mark.asyncio
async def test_events_filtering_and_filters_endpoint(test_env: Settings):
    db = Database(test_env)
    await db.connect()

    clip1 = test_env.processed_dir / "Backyard" / "2026-08-30" / "clip1.mp4"
    clip1.parent.mkdir(parents=True, exist_ok=True)
    clip1.write_text("dummy")

    thumb1 = test_env.thumbnails_dir / "2026-08-30" / "Backyard" / "clip1.gif"
    thumb1.parent.mkdir(parents=True, exist_ok=True)
    thumb1.write_text("gif")

    clip2 = test_env.processed_dir / "Frontdoor" / "2026-08-31" / "clip2.mp4"
    clip2.parent.mkdir(parents=True, exist_ok=True)
    clip2.write_text("dummy")

    det1 = [DetectionItem(0, "person", 0.95, 1.0, [0, 0, 1, 1], str(thumb1))]
    det2 = [DetectionItem(2, "car", 0.88, 2.0, [0, 0, 1, 1], None)]

    ev1 = await db.save_event(clip1, det1, camera_name="Backyard", detected_at="2026-08-30T10:00:00-07:00")
    ev2 = await db.save_event(clip2, det2, camera_name="Frontdoor", detected_at="2026-08-31T15:30:00-07:00")

    app = create_app(test_env)
    # inject pre-populated db
    app.state.db = db

    with TestClient(app) as client:
        # 1. Filters endpoint
        res_f = client.get("/api/v1/events/filters")
        assert res_f.status_code == 200
        f_data = res_f.json()
        assert "2026-08-30" in f_data["dates"]
        assert "2026-08-31" in f_data["dates"]
        assert "Backyard" in f_data["cameras"]
        assert "Frontdoor" in f_data["cameras"]

        # 2. Filter by date
        res_d = client.get("/api/v1/events?date=2026-08-31")
        assert res_d.status_code == 200
        assert len(res_d.json()["events"]) == 1
        assert res_d.json()["events"][0]["id"] == ev2.id

        # 3. Filter by camera
        res_c = client.get("/api/v1/events?camera=Backyard")
        assert res_c.status_code == 200
        assert len(res_c.json()["events"]) == 1
        assert res_c.json()["events"][0]["id"] == ev1.id

        # 4. Filter by object class
        res_cls = client.get("/api/v1/events?cls=person")
        assert res_cls.status_code == 200
        assert len(res_cls.json()["events"]) == 1
        assert res_cls.json()["events"][0]["id"] == ev1.id

        # 5. Video download header
        res_v = client.get(f"/api/v1/events/{ev1.id}/video?download=1")
        assert res_v.status_code == 200
        assert "attachment" in res_v.headers.get("content-disposition", "")

        # 6. Thumbnail download header
        res_t = client.get(f"/api/v1/events/{ev1.id}/thumbnail?download=1")
        assert res_t.status_code == 200
        assert "attachment" in res_t.headers.get("content-disposition", "")

    await db.close()


@pytest.mark.asyncio
async def test_rescan_recovers_unindexed_clips(test_env: Settings):
    db = Database(test_env)
    await db.connect()

    # Create an unindexed clip in processed_dir with detection suffix
    unindexed = test_env.processed_dir / "Backdoor" / "2026-08-31" / "CAM0200000002_0000046a_20260831_200809-1person.mp4"
    unindexed.parent.mkdir(parents=True, exist_ok=True)
    unindexed.write_text("dummy video")

    thumb = test_env.thumbnails_dir / "2026-08-31" / "Backdoor" / "CAM0200000002_0000046a_20260831_200809-1person.gif"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_text("dummy gif")

    recovered = await db.recover_unindexed_events()
    assert recovered == 1

    events = await db.get_events()
    assert len(events) == 1
    assert "CAM0200000002_0000046a_20260831_200809-1person.mp4" in events[0].clip_path
    assert events[0].objects[0]["class"] == "person"

    await db.close()


def test_healthz_requires_auth(test_env: Settings):
    test_env.allowed_google_emails = ["user@example.com"]
    test_env.allow_lan_auth_bypass = False
    app = create_app(test_env)
    with TestClient(app) as client:
        # Unauthenticated -> 401
        res = client.get("/healthz")
        assert res.status_code == 401


def test_ui_serves_self_hosted_static_assets(test_env: Settings):
    app = create_app(test_env)
    with TestClient(app) as client:
        # 1. Web UI Dashboard HTML references local static assets, not CDNs
        res_html = client.get("/")
        assert res_html.status_code == 200
        assert "/static/css/tailwind.min.css" in res_html.text
        assert "/static/vendor/fontawesome/css/all.min.css" in res_html.text
        assert "cdn.tailwindcss.com" not in res_html.text
        assert "cdnjs.cloudflare.com" not in res_html.text

        # 2. Local Tailwind CSS is served
        res_tw = client.get("/static/css/tailwind.min.css")
        assert res_tw.status_code == 200
        assert "text/css" in res_tw.headers.get("content-type", "")

        # 3. Local FontAwesome CSS and fonts are served
        res_fa = client.get("/static/vendor/fontawesome/css/all.min.css")
        assert res_fa.status_code == 200
        assert "text/css" in res_fa.headers.get("content-type", "")

        res_font = client.get("/static/vendor/fontawesome/webfonts/fa-solid-900.woff2")
        assert res_font.status_code == 200
