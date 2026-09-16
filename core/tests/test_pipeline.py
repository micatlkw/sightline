import asyncio
from pathlib import Path
import pytest
from app.config import Settings
from app.core.event_bus import EventBus
from app.core.pipeline import Pipeline
from app.database import Database
from app.models.event import DetectionItem, EventRecord
from app.notifier.base import BaseNotifier
from app.watcher.directory_watcher import DirectoryWatcher


class MockDetector:
    def __init__(self, detections=None):
        self.detections = detections or []

    def detect_sync(self, clip_path: Path, *args, **kwargs):
        return self.detections



class RecordingNotifier(BaseNotifier):
    def __init__(self):
        self.events = []

    async def notify(self, event: EventRecord) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_pipeline_process_clip_with_detections(test_settings: Settings, test_db: Database, temp_dir: Path):
    bus = EventBus()
    clip_queue = asyncio.Queue()
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()

    thumb = temp_dir / "thumbnails" / "thumb1.jpg"
    thumb.touch()

    dets = [
        DetectionItem(
            class_id=0,
            class_name="person",
            confidence=0.92,
            timestamp_sec=2.0,
            bbox=[0.1, 0.1, 0.5, 0.5],
            keyframe_path=str(thumb),
        )
    ]
    detector = MockDetector(dets)

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    det_sub = bus.subscribe("detection")
    done_sub = bus.subscribe("clip.done")

    # Create dummy video file
    clip_file = test_settings.watch_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    clip_file.parent.mkdir(parents=True, exist_ok=True)
    clip_file.write_bytes(b"dummy video bytes")

    # Process
    await pipeline.start()
    await pipeline.enqueue(clip_file)

    # Await bus events
    det_event = await asyncio.wait_for(det_sub.get(), timeout=5.0)
    assert det_event["camera_name"] == "CAM0100000001"
    assert det_event["objects"][0]["class"] == "person"

    done_event = await asyncio.wait_for(done_sub.get(), timeout=5.0)
    expected_processed = (
        test_settings.processed_dir
        / "2026-08-28"
        / "CAM0100000001"
        / "CAM0100000001_000000e2_20260828_122954-1person.mp4"
    )
    assert done_event["path"] == str(expected_processed)
    assert expected_processed.exists()
    assert not clip_file.exists()

    # Verify notifier received event
    assert len(notifier.events) == 1
    assert notifier.events[0].camera_name == "CAM0100000001"

    # Verify DB state
    clips = await test_db.get_clips()
    assert len(clips) == 1
    assert clips[0].status == "done"
    assert clips[0].path == str(expected_processed)

    events = await test_db.get_events()
    assert len(events) == 1
    assert events[0].camera_name == "CAM0100000001"
    assert events[0].clip_path == str(expected_processed)
    await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_scan_and_enqueue_filtering_and_order(test_settings: Settings, test_db: Database, temp_dir: Path):
    bus = EventBus()
    clip_queue = asyncio.Queue()
    test_settings.scan_on_startup = False
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()
    detector = MockDetector([])

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    incoming = test_settings.incoming_dir
    incoming.mkdir(parents=True, exist_ok=True)

    # 1. Existing clip marked done in DB (should be ignored)
    clip_done = incoming / "camera1" / "done_clip.mp4"
    clip_done.parent.mkdir(parents=True, exist_ok=True)
    clip_done.write_bytes(b"done content")
    await test_db.mark_clip(clip_done, "done")

    # 2. Interrupted clip in pending state (should be picked up)
    clip_interrupted = incoming / "camera1" / "interrupted_clip.mp4"
    clip_interrupted.write_bytes(b"interrupted content")
    await test_db.mark_clip(clip_interrupted, "pending")

    # 3. New unrecorded clips with specific mtimes
    clip_old = incoming / "camera2" / "old.mp4"
    clip_old.parent.mkdir(parents=True, exist_ok=True)
    clip_old.write_bytes(b"old")

    clip_new = incoming / "camera1" / "new.mp4"
    clip_new.write_bytes(b"new")

    # Non-mp4 file (should be ignored)
    txt_file = incoming / "readme.txt"
    txt_file.write_text("not a video")

    import os
    now = 1000000.0
    os.utime(clip_old, (now, now))
    os.utime(clip_interrupted, (now + 100, now + 100))
    os.utime(clip_new, (now + 200, now + 200))

    enqueued = await pipeline.scan_and_enqueue()

    # Verify done_clip and non-mp4 were skipped, and remaining are ordered chronologically
    assert len(enqueued) == 3
    assert enqueued == [clip_old, clip_interrupted, clip_new]
    assert clip_queue.qsize() == 3


@pytest.mark.asyncio
async def test_pipeline_startup_scan_enabled(test_settings: Settings, test_db: Database, temp_dir: Path):
    bus = EventBus()
    clip_queue = asyncio.Queue()
    test_settings.scan_on_startup = True
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()
    detector = MockDetector([])

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    done_sub = bus.subscribe("clip.done")

    # Create backlog clip before pipeline start
    backlog_clip = test_settings.incoming_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    backlog_clip.parent.mkdir(parents=True, exist_ok=True)
    backlog_clip.write_bytes(b"backlog video")

    # Start pipeline (should auto-scan and process)
    await pipeline.start()

    done_event = await asyncio.wait_for(done_sub.get(), timeout=5.0)
    expected_processed = (
        test_settings.processed_dir
        / "2026-08-28"
        / "CAM0100000001"
        / "CAM0100000001_000000e2_20260828_122954-none.mp4"
    )
    assert done_event["path"] == str(expected_processed)
    assert expected_processed.exists()
    assert not backlog_clip.exists()

    await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_startup_scan_disabled(test_settings: Settings, test_db: Database, temp_dir: Path):
    bus = EventBus()
    clip_queue = asyncio.Queue()
    test_settings.scan_on_startup = False
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()
    detector = MockDetector([])

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    # Create backlog clip before pipeline start
    backlog_clip = test_settings.incoming_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    backlog_clip.parent.mkdir(parents=True, exist_ok=True)
    backlog_clip.write_bytes(b"backlog video")

    await pipeline.start()
    assert clip_queue.empty()
    assert backlog_clip.exists()

    await pipeline.stop()


class ErrorDetector:
    def detect_sync(self, clip_path: Path):
        raise ValueError(f"Failed to open video {clip_path.name}: file is unreadable, corrupted, or missing moov atom")


@pytest.mark.asyncio
async def test_pipeline_corrupt_file_moved_to_corrupt(test_settings: Settings, test_db: Database, temp_dir: Path):
    bus = EventBus()
    clip_queue = asyncio.Queue()
    test_settings.scan_on_startup = False
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()
    detector = ErrorDetector()

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    error_sub = bus.subscribe("clip.error")

    # Create dummy corrupt clip in incoming
    corrupt_clip = test_settings.incoming_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    corrupt_clip.parent.mkdir(parents=True, exist_ok=True)
    corrupt_clip.write_bytes(b"corrupt video data")

    await pipeline.start()
    await pipeline.enqueue(corrupt_clip)

    err_event = await asyncio.wait_for(error_sub.get(), timeout=5.0)
    expected_corrupt = (
        test_settings.processed_dir
        / "corrupt"
        / "2026-08-28"
        / "CAM0100000001"
        / "CAM0100000001_000000e2_20260828_122954.mp4"
    )

    assert err_event["path"] == str(expected_corrupt)
    assert err_event["incoming_path"] == str(corrupt_clip)
    assert "corrupted" in err_event["error"]

    assert expected_corrupt.exists()
    assert not corrupt_clip.exists()

    clips = await test_db.get_clips()
    assert len(clips) == 1
    assert clips[0].status == "error"
    assert clips[0].path == str(expected_corrupt)

    await pipeline.stop()


class UnstableWatcher(DirectoryWatcher):
    async def wait_for_stable(self, path: Path) -> bool:
        return False


@pytest.mark.asyncio
async def test_pipeline_unstabilized_file_moved_to_corrupt(test_settings: Settings, test_db: Database, temp_dir: Path):
    bus = EventBus()
    clip_queue = asyncio.Queue()
    test_settings.scan_on_startup = False
    watcher = UnstableWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()
    detector = MockDetector([])

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    error_sub = bus.subscribe("clip.error")

    unstable_clip = test_settings.incoming_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    unstable_clip.parent.mkdir(parents=True, exist_ok=True)
    unstable_clip.write_bytes(b"partial video data")

    await pipeline.start()
    await pipeline.enqueue(unstable_clip)

    err_event = await asyncio.wait_for(error_sub.get(), timeout=5.0)
    expected_corrupt = (
        test_settings.processed_dir
        / "corrupt"
        / "2026-08-28"
        / "CAM0100000001"
        / "CAM0100000001_000000e2_20260828_122954.mp4"
    )

    assert err_event["path"] == str(expected_corrupt)
    assert expected_corrupt.exists()
    assert not unstable_clip.exists()

    clips = await test_db.get_clips()
    assert len(clips) == 1
    assert clips[0].status == "error"
    assert clips[0].path == str(expected_corrupt)

    await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_multiple_detections_formatting(test_settings: Settings, test_db: Database, temp_dir: Path):
    bus = EventBus()
    clip_queue = asyncio.Queue()
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()

    dets = [
        DetectionItem(
            class_id=0,
            class_name="person",
            confidence=0.95,
            timestamp_sec=1.0,
            bbox=[0.1, 0.1, 0.5, 0.5],
        ),
        DetectionItem(
            class_id=0,
            class_name="person",
            confidence=0.90,
            timestamp_sec=1.0,
            bbox=[0.5, 0.1, 0.9, 0.5],
        ),
        DetectionItem(
            class_id=2,
            class_name="car",
            confidence=0.88,
            timestamp_sec=2.0,
            bbox=[0.2, 0.2, 0.8, 0.8],
        ),
    ]
    detector = MockDetector(dets)

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    done_sub = bus.subscribe("clip.done")

    clip_file = test_settings.watch_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    clip_file.parent.mkdir(parents=True, exist_ok=True)
    clip_file.write_bytes(b"dummy video bytes")

    await pipeline.start()
    await pipeline.enqueue(clip_file)

    done_event = await asyncio.wait_for(done_sub.get(), timeout=5.0)
    expected_processed = (
        test_settings.processed_dir
        / "2026-08-28"
        / "CAM0100000001"
        / "CAM0100000001_000000e2_20260828_122954-2person-1car.mp4"
    )
    assert done_event["path"] == str(expected_processed)
    assert expected_processed.exists()

    events = await test_db.get_events()
    assert len(events) == 1
    assert events[0].camera_name == "CAM0100000001"
    assert events[0].clip_path == str(expected_processed)

    await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_camera_name_mapping(test_settings: Settings, test_db: Database, temp_dir: Path):
    import json
    cfg_file = temp_dir / "cameras_name.json"
    cfg_file.write_text(json.dumps([
        {"name": "Backyard", "serial": "CAM0100000001"}
    ]))
    test_settings.cameras_config_path = cfg_file

    bus = EventBus()
    clip_queue = asyncio.Queue()
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()

    dets = [
        DetectionItem(
            class_id=0,
            class_name="person",
            confidence=0.92,
            timestamp_sec=1.0,
            bbox=[0.1, 0.1, 0.5, 0.5],
        )
    ]
    detector = MockDetector(dets)

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    det_sub = bus.subscribe("detection")
    done_sub = bus.subscribe("clip.done")

    clip_file = test_settings.watch_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    clip_file.parent.mkdir(parents=True, exist_ok=True)
    clip_file.write_bytes(b"dummy video bytes")

    await pipeline.start()
    await pipeline.enqueue(clip_file)

    det_event = await asyncio.wait_for(det_sub.get(), timeout=5.0)
    assert det_event["camera_name"] == "Backyard"

    done_event = await asyncio.wait_for(done_sub.get(), timeout=5.0)
    expected_processed = (
        test_settings.processed_dir
        / "2026-08-28"
        / "Backyard"
        / "CAM0100000001_000000e2_20260828_122954-1person.mp4"
    )
    assert done_event["path"] == str(expected_processed)
    assert expected_processed.exists()

    # Verify notifier received event with camera_name Backyard
    assert len(notifier.events) == 1
    assert notifier.events[0].camera_name == "Backyard"

    # Verify DB
    events = await test_db.get_events()
    assert len(events) == 1
    assert events[0].camera_name == "Backyard"
    assert events[0].clip_path == str(expected_processed)

    await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_per_camera_disabled_skips_detection(test_settings: Settings, test_db: Database, temp_dir: Path):
    from app.config import CameraConfig

    test_settings.cameras = [
        CameraConfig(name="Unused", serial="CAM0300000003", enabled=False),
        CameraConfig(name="Frontdoor", serial="CAM0200000002", enabled=True),
    ]

    bus = EventBus()
    clip_queue = asyncio.Queue()
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()

    dets = [
        DetectionItem(class_id=0, class_name="person", confidence=0.92, timestamp_sec=1.0, bbox=[0.1, 0.1, 0.5, 0.5])
    ]
    detector = MockDetector(dets)

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    done_sub = bus.subscribe("clip.done")

    clip_file = test_settings.watch_dir / "CAM0300000003_000000e2_20260828_122954.mp4"
    clip_file.parent.mkdir(parents=True, exist_ok=True)
    clip_file.write_bytes(b"dummy video bytes")

    await pipeline.start()
    await pipeline.enqueue(clip_file)

    done_event = await asyncio.wait_for(done_sub.get(), timeout=5.0)
    assert done_event.get("skipped") is True
    # Notifier should NOT have received events
    assert len(notifier.events) == 0

    await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_logs_notification_delay_and_cooldown(test_settings: Settings, test_db: Database, caplog):
    import logging
    from app.notifier.composite import CompositeNotifier

    bus = EventBus()
    clip_queue = asyncio.Queue()
    watcher = DirectoryWatcher(test_settings, clip_queue)
    mock_notif = RecordingNotifier()
    composite_notifier = CompositeNotifier([mock_notif], cooldown_seconds=60)

    dets = [
        DetectionItem(class_id=0, class_name="person", confidence=0.95, timestamp_sec=1.0, bbox=[0.1, 0.1, 0.5, 0.5])
    ]
    detector = MockDetector(dets)

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=composite_notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    clip1 = test_settings.watch_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    clip1.parent.mkdir(parents=True, exist_ok=True)
    clip1.write_bytes(b"dummy video bytes 1")

    await pipeline.start()

    with caplog.at_level(logging.INFO):
        done_sub = bus.subscribe("clip.done")
        await pipeline.enqueue(clip1)
        await asyncio.wait_for(done_sub.get(), timeout=5.0)

    # First clip: notification dispatched
    assert any("notification delay:" in record.message for record in caplog.records)

    # Second clip immediately after on same camera: cooldown active
    clip2 = test_settings.watch_dir / "CAM0100000001_000000e3_20260828_123000.mp4"
    clip2.write_bytes(b"dummy video bytes 2")

    with caplog.at_level(logging.INFO):
        caplog.clear()
        done_sub2 = bus.subscribe("clip.done")
        await pipeline.enqueue(clip2)
        await asyncio.wait_for(done_sub2.get(), timeout=5.0)

    assert any("notification: cooldown active, delay:" in record.message for record in caplog.records)

    await pipeline.stop()


class EarlyMockDetector:
    def __init__(self, detections, early_detections=None):
        self.detections = detections
        self.early_detections = early_detections or detections

    def detect_sync(self, clip_path: Path, *args, **kwargs):
        on_early = kwargs.get("on_early_detection")
        if not on_early and len(args) >= 6:
            on_early = args[5]
        if on_early and self.early_detections:
            import numpy as np
            dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
            on_early(self.early_detections, dummy_frame, 0.5)
        return self.detections, []

    def _save_snapshot_thumbnail(self, frame, clip_path, detections, camera_name=None):
        return str(clip_path.parent / f"{clip_path.stem}.jpg")

    def render_full_clip_gif(self, frames, clip_path, detections, camera_name=None, **kwargs):
        return str(clip_path.parent / f"{clip_path.stem}.gif")


@pytest.mark.asyncio
async def test_pipeline_concurrency_and_early_alert(test_settings: Settings, test_db: Database, temp_dir: Path):
    import time
    test_settings.pipeline_concurrency = 2
    bus = EventBus()
    clip_queue = asyncio.Queue()
    watcher = DirectoryWatcher(test_settings, clip_queue)
    notifier = RecordingNotifier()

    dets = [
        DetectionItem(class_id=2, class_name="car", confidence=0.85, timestamp_sec=0.5, bbox=[0.1, 0.1, 0.4, 0.4]),
        DetectionItem(class_id=2, class_name="car", confidence=0.92, timestamp_sec=2.0, bbox=[0.1, 0.1, 0.5, 0.5]),
    ]
    early_dets = [dets[0]]
    detector = EarlyMockDetector(detections=dets, early_detections=early_dets)

    pipeline = Pipeline(
        settings=test_settings,
        db=test_db,
        detector=detector,
        watcher=watcher,
        notifier=notifier,
        bus=bus,
        clip_queue=clip_queue,
    )

    assert pipeline._concurrency == 2
    await pipeline.start()
    assert len(pipeline._worker_tasks) == 2

    done_sub = bus.subscribe("clip.done")
    clip = test_settings.watch_dir / "CAM0100000001_00000bf5_20260905_175242.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"dummy video bytes")

    # Enqueue as tuple (clip_path, enqueued_at)
    await clip_queue.put((clip, time.time()))

    await asyncio.wait_for(done_sub.get(), timeout=5.0)

    # Notifier received early alert
    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.camera_name == "CAM0100000001"

    # DB event was updated with full detections
    events = await test_db.get_events()
    assert len(events) == 1
    assert len(events[0].objects) == 2

    await pipeline.stop()



