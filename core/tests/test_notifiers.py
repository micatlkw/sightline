import asyncio
import pytest
from app.models.event import EventRecord
from app.notifier.base import BaseNotifier
from app.notifier.composite import CompositeNotifier
from app.notifier.apprise_notifier import AppriseNotifier


class MockRecordingNotifier(BaseNotifier):
    def __init__(self):
        self.received_events = []

    async def notify(self, event: EventRecord) -> None:
        self.received_events.append(event)


class FailingNotifier(BaseNotifier):
    async def notify(self, event: EventRecord) -> None:
        raise RuntimeError("Simulated network failure")


@pytest.mark.asyncio
async def test_composite_notifier_cooldown():
    mock_notif = MockRecordingNotifier()
    composite = CompositeNotifier([mock_notif], cooldown_seconds=2)

    event_front1 = EventRecord(
        id=1,
        clip_path="/data/front.mp4",
        camera_name="frontdoor",
        detected_at="2026-08-23T10:00:00Z",
        objects=[{"class": "person"}],
        thumbnail=None,
    )
    event_front2 = EventRecord(
        id=2,
        clip_path="/data/front2.mp4",
        camera_name="frontdoor",
        detected_at="2026-08-23T10:00:01Z",
        objects=[{"class": "car"}],
        thumbnail=None,
    )
    event_back = EventRecord(
        id=3,
        clip_path="/data/back.mp4",
        camera_name="backyard",
        detected_at="2026-08-23T10:00:01Z",
        objects=[{"class": "dog"}],
        thumbnail=None,
    )

    # 1. First event on frontdoor should notify
    res1 = await composite.notify(event_front1)
    assert res1 is True
    assert len(mock_notif.received_events) == 1
    assert mock_notif.received_events[0].id == 1

    # 2. Immediate second event on frontdoor should be suppressed by cooldown
    res2 = await composite.notify(event_front2)
    assert res2 is False
    assert len(mock_notif.received_events) == 1

    # 3. Event on backyard should notify immediately (separate camera cooldown)
    res3 = await composite.notify(event_back)
    assert res3 is True
    assert len(mock_notif.received_events) == 2
    assert mock_notif.received_events[1].id == 3

    # 4. Wait for cooldown expiration on frontdoor
    await asyncio.sleep(2.1)
    res4 = await composite.notify(event_front2)
    assert res4 is True
    assert len(mock_notif.received_events) == 3
    assert mock_notif.received_events[2].id == 2



@pytest.mark.asyncio
async def test_composite_notifier_handles_child_error():
    mock_notif = MockRecordingNotifier()
    fail_notif = FailingNotifier()
    composite = CompositeNotifier([fail_notif, mock_notif], cooldown_seconds=0)

    event = EventRecord(
        id=1,
        clip_path="/data/clip.mp4",
        camera_name="driveway",
        detected_at="2026-08-23T10:00:00Z",
        objects=[{"class": "truck"}],
        thumbnail=None,
    )

    # Should not raise exception and should still call other notifiers
    await composite.notify(event)
    assert len(mock_notif.received_events) == 1


@pytest.mark.asyncio
async def test_apprise_notifier_initialization():
    # Empty URLs
    notifier = AppriseNotifier([])
    assert len(notifier._ap) == 0

    # Valid URL syntax
    notifier2 = AppriseNotifier(["json://localhost:8000/webhook"])
    assert len(notifier2._ap) == 1


@pytest.mark.asyncio
async def test_apprise_notifier_with_thumbnail_attachment(tmp_path):
    from unittest.mock import AsyncMock, patch

    notifier = AppriseNotifier(["json://localhost:8000/webhook"])
    thumb_file = tmp_path / "thumb.gif"
    thumb_file.write_bytes(b"GIF89a")

    event = EventRecord(
        id=42,
        clip_path="/data/clip.mp4",
        camera_name="frontdoor",
        detected_at="2026-08-30T10:00:00Z",
        objects=[{"class": "person"}],
        thumbnail=str(thumb_file),
    )

    with patch.object(notifier._ap, "async_notify", new_callable=AsyncMock) as mock_notify:
        mock_notify.return_value = True
        await notifier.notify(event)
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["attach"] == [str(thumb_file)]
        body = mock_notify.call_args.kwargs["body"]
        assert "Date:" in body
        assert "Time:" in body
        assert "detection" not in body.lower()


@pytest.mark.asyncio
async def test_composite_update_settings():
    apprise_notif = AppriseNotifier([])
    composite = CompositeNotifier([apprise_notif], cooldown_seconds=60)

    class DummySettings:
        alert_cooldown_seconds = 15
        apprise_urls = ["json://localhost:8000/webhook"]

    composite.update_settings(DummySettings())
    assert composite._cooldown == 15
    assert len(apprise_notif._ap) == 1


def test_apprise_notifier_blocks_disallowed_schemes(tmp_path):
    evil_file = tmp_path / "evil.txt"
    evil_url = f"file://{evil_file}"
    notifier = AppriseNotifier([evil_url, "json://localhost:8000/webhook"])
    # evil_url is filtered out and skipped; only json:// is registered
    assert len(notifier._ap) == 1

    # Dynamically updating URLs also rejects file:// and invalid schemes
    notifier.update_urls(["file:///tmp/other.txt", "ntfys://ntfy.sh/test"])
    assert len(notifier._ap) == 1


def test_sanitize_webpush_topic():
    from app.notifier.webpush import sanitize_webpush_topic

    # Standard names
    assert sanitize_webpush_topic("Backyard") == "cam-Backyard"
    assert sanitize_webpush_topic("Frontyard East") == "cam-Frontyard-East"

    # Special characters and punctuation
    assert sanitize_webpush_topic("Driveway #1 (4K) @ Main") == "cam-Driveway-1-4K-Main"

    # RFC 8030 length limit: exactly 32 chars maximum
    long_name = "Camera-Located-At-The-Very-Far-End-Of-The-North-Driveway"
    topic = sanitize_webpush_topic(long_name)
    assert len(topic) <= 32
    assert topic == "cam-Camera-Located-At-The-Very-"

    # Empty / whitespace / None fallback
    assert sanitize_webpush_topic(None) == "cam-default"
    assert sanitize_webpush_topic("") == "cam-default"
    assert sanitize_webpush_topic("   ") == "cam-default"

    # Custom prefix
    assert sanitize_webpush_topic("test", prefix="") == "test"


@pytest.mark.asyncio
async def test_webpush_notifier_topic_modes(tmp_path):
    from unittest.mock import AsyncMock, patch
    from app.notifier.webpush_notifier import WebPushNotifier

    class DummyDB:
        async def get_web_push_subscriptions(self, user_email=None):
            return [{"endpoint": "https://fcm.googleapis.com/fcm/send/fake", "p256dh": "key", "auth": "auth"}]

    class DummySettings:
        db_path = str(tmp_path / "sightline.db")
        acme_email = "admin@example.com"
        webpush_topic_mode = "camera"
        webpush_ttl_seconds = 7200

    db = DummyDB()
    settings = DummySettings()
    notifier = WebPushNotifier(db=db, settings=settings)
    notifier._privkey = "mock_key"
    notifier._pubkey_b64 = "mock_pub"

    event = EventRecord(
        id=10,
        clip_path="/data/front.mp4",
        camera_name="Frontyard East",
        detected_at="2026-09-08T10:00:00Z",
        objects=[{"class": "person", "confidence": 0.9}],
        thumbnail=None,
    )

    # 1. Camera mode: uses cam-Frontyard-East
    with patch("app.notifier.webpush_notifier.send_web_push", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = 201
        await notifier.notify(event)
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["topic"] == "cam-Frontyard-East"
        assert mock_send.call_args.kwargs["ttl"] == 7200

    # 2. Global mode: uses sightline-alert
    settings.webpush_topic_mode = "global"
    notifier.update_settings(settings)
    assert notifier._topic_mode == "global"

    with patch("app.notifier.webpush_notifier.send_web_push", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = 201
        await notifier.notify(event)
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["topic"] == "sightline-alert"

    # 3. Disabled mode: topic is None
    settings.webpush_topic_mode = "disabled"
    notifier.update_settings(settings)
    assert notifier._topic_mode == "disabled"

    with patch("app.notifier.webpush_notifier.send_web_push", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = 201
        await notifier.notify(event)
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["topic"] is None

    # 4. Test notification: uses sightline-test
    settings.webpush_topic_mode = "camera"
    notifier.update_settings(settings)
    with patch("app.notifier.webpush_notifier.send_web_push", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = 201
        res = await notifier.send_test_notification()
        assert res["success"] is True
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["topic"] == "sightline-test"


@pytest.mark.asyncio
async def test_composite_propagates_to_webpush(tmp_path):
    from app.notifier.webpush_notifier import WebPushNotifier

    class DummyDB:
        pass

    class DummySettings:
        db_path = str(tmp_path / "sightline.db")
        acme_email = "admin@example.com"
        alert_cooldown_seconds = 30
        webpush_topic_mode = "global"
        webpush_ttl_seconds = 3600

    webpush = WebPushNotifier(db=DummyDB(), settings=DummySettings())
    assert webpush._topic_mode == "global"
    assert webpush._ttl == 3600

    composite = CompositeNotifier([webpush], cooldown_seconds=60)

    class UpdatedSettings:
        alert_cooldown_seconds = 15
        webpush_topic_mode = "camera"
        webpush_ttl_seconds = 86400

    composite.update_settings(UpdatedSettings())
    assert composite._cooldown == 15
    assert webpush._topic_mode == "camera"
    assert webpush._ttl == 86400




