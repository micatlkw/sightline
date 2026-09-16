from pathlib import Path
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.models.event import DetectionItem
from app.main import create_app


def test_video_streaming_full_and_partial_content(test_settings: Settings, temp_dir: Path):
    # 1. Create a dummy video file
    video_file = test_settings.processed_dir / "frontdoor_test.mp4"
    data = b"0123456789" * 100  # 1000 bytes
    video_file.write_bytes(data)

    app = create_app(test_settings)
    with TestClient(app) as client:
        # Save event in DB
        db: Database = app.state.db
        import asyncio
        async def insert_event():
            return await db.save_event(
                clip_path=video_file,
                detections=[
                    DetectionItem(
                        class_id=0,
                        class_name="person",
                        confidence=0.9,
                        timestamp_sec=1.0,
                        bbox=[0.1, 0.1, 0.5, 0.5],
                    )
                ],
                camera_name="frontdoor",
            )
        event = asyncio.run(insert_event())

        # Test full GET request
        res = client.get(f"/events/{event.id}/video")
        assert res.status_code == 200
        assert res.headers["accept-ranges"] == "bytes"
        assert res.content == data

        # Test HTTP 206 Partial Content (first 10 bytes: 0-9)
        res_range = client.get(
            f"/events/{event.id}/video",
            headers={"Range": "bytes=0-9"},
        )
        assert res_range.status_code == 206
        assert res_range.headers["content-range"] == "bytes 0-9/1000"
        assert res_range.headers["content-length"] == "10"
        assert res_range.content == b"0123456789"

        # Test HTTP 206 Partial Content (middle bytes: 50-99)
        res_range2 = client.get(
            f"/events/{event.id}/video",
            headers={"Range": "bytes=50-99"},
        )
        assert res_range2.status_code == 206
        assert res_range2.headers["content-range"] == "bytes 50-99/1000"
        assert res_range2.headers["content-length"] == "50"
        assert res_range2.content == data[50:100]
