from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from app.database import Database
from app.models.event import EventRecord
from app.notifier.firebase_notifier import FirebaseNotifier


@pytest.mark.asyncio
async def test_firebase_notifier_disabled_without_credentials(test_db: Database, tmp_path: Path):
    non_existent = tmp_path / "missing_firebase.json"
    notifier = FirebaseNotifier(non_existent, test_db)
    await notifier.startup()
    assert notifier._initialized is False

    event = EventRecord(
        id=1,
        clip_path="/data/processed/test.mp4",
        camera_name="frontdoor",
        detected_at="2026-08-23T18:00:00Z",
        objects=[{"class_id": 0, "class": "person", "confidence": 0.95, "timestamp_sec": 1.0, "bbox": [0,0,1,1], "keyframe_path": None}],
        thumbnail=None,
        notified=0,
    )
    # Should not raise
    await notifier.notify(event)
    await notifier.shutdown()


@pytest.mark.asyncio
async def test_firebase_notifier_dispatches_multicast(test_db: Database, tmp_path: Path):
    cred_file = tmp_path / "firebase_creds.json"
    cred_file.write_text('{"type": "service_account"}')

    # Register 2 devices
    await test_db.register_device("alice@gmail.com", "dev1", "fcm_token_1", "Pixel")
    await test_db.register_device("bob@gmail.com", "dev2", "fcm_token_2", "Galaxy")

    notifier = FirebaseNotifier(cred_file, test_db)
    notifier._initialized = True
    notifier._app = MagicMock()

    event = EventRecord(
        id=99,
        clip_path="/data/processed/frontdoor/clip.mp4",
        camera_name="frontdoor",
        detected_at="2026-08-23T18:00:00Z",
        objects=[
            {"class_id": 0, "class": "person", "confidence": 0.95, "timestamp_sec": 1.0, "bbox": [0,0,1,1], "keyframe_path": None},
            {"class_id": 16, "class": "dog", "confidence": 0.88, "timestamp_sec": 1.0, "bbox": [0,0,1,1], "keyframe_path": None},
        ],
        thumbnail=None,
        notified=0,
    )

    mock_response = MagicMock()
    mock_response.success_count = 2
    mock_response.responses = [MagicMock(success=True), MagicMock(success=True)]

    with patch("firebase_admin.messaging.send_each_for_multicast", return_value=mock_response) as mock_send:
        await notifier.notify(event)
        assert mock_send.called
        msg_arg = mock_send.call_args[0][0]
        assert "fcm_token_1" in msg_arg.tokens
        assert "fcm_token_2" in msg_arg.tokens
        assert msg_arg.data["camera_name"] == "frontdoor"
        assert "Person" in msg_arg.notification.title


@pytest.mark.asyncio
async def test_firebase_notifier_with_gif_thumbnail(test_db: Database, tmp_path: Path):
    from PIL import Image

    cred_file = tmp_path / "firebase_creds.json"
    cred_file.write_text('{"type": "service_account"}')

    await test_db.register_device("alice@gmail.com", "dev1", "fcm_token_1", "Pixel")

    thumb_gif = tmp_path / "thumb.gif"
    img = Image.new("RGB", (640, 480), color="blue")
    img.save(thumb_gif, format="GIF")

    notifier = FirebaseNotifier(cred_file, test_db)
    notifier._initialized = True
    notifier._app = MagicMock()

    event = EventRecord(
        id=100,
        clip_path="/data/processed/frontdoor/clip.mp4",
        camera_name="frontdoor",
        detected_at="2026-08-23T18:00:00Z",
        objects=[
            {"class_id": 0, "class": "person", "confidence": 0.95, "timestamp_sec": 1.0, "bbox": [0,0,1,1], "keyframe_path": str(thumb_gif)},
        ],
        thumbnail=str(thumb_gif),
        notified=0,
    )

    mock_response = MagicMock()
    mock_response.success_count = 1
    mock_response.responses = [MagicMock(success=True)]

    with patch("firebase_admin.messaging.send_each_for_multicast", return_value=mock_response) as mock_send:
        await notifier.notify(event)
        assert mock_send.called
        msg_arg = mock_send.call_args[0][0]
        assert msg_arg.data["preview_b64"] != ""

