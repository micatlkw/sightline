from pathlib import Path
import pytest
from app.database import Database
from app.models.event import DetectionItem


@pytest.mark.asyncio
async def test_database_lifecycle(test_db: Database):
    # Database is already connected via fixture
    assert test_db._conn is not None


@pytest.mark.asyncio
async def test_clip_tracking(test_db: Database, temp_dir: Path):
    clip_path = temp_dir / "clips" / "frontdoor" / "clip1.mp4"

    # 1. Mark pending
    await test_db.mark_clip(clip_path, "pending")
    clips = await test_db.get_clips()
    assert len(clips) == 1
    assert clips[0].path == str(clip_path)
    assert clips[0].status == "pending"
    assert clips[0].processed_at is None

    # 2. Mark processing
    await test_db.mark_clip(clip_path, "processing")
    clips = await test_db.get_clips()
    assert clips[0].status == "processing"

    # 3. Mark done
    await test_db.mark_clip(clip_path, "done")
    clips = await test_db.get_clips()
    assert clips[0].status == "done"
    assert clips[0].processed_at is not None

    # 4. Mark error with message
    err_clip = temp_dir / "clips" / "backyard" / "corrupt.mp4"
    await test_db.mark_clip(err_clip, "error", error_msg="corrupt header")
    clips = await test_db.get_clips()
    assert len(clips) == 2
    err_record = next(c for c in clips if c.path == str(err_clip))
    assert err_record.status == "error"
    assert err_record.error_msg == "corrupt header"

    # 5. Check get_done_clip_paths
    done_paths = await test_db.get_done_clip_paths()
    assert done_paths == {str(clip_path)}
    assert str(err_clip) not in done_paths


@pytest.mark.asyncio
async def test_save_and_get_events(test_db: Database, temp_dir: Path):
    clip_path = temp_dir / "clips" / "frontdoor" / "20260823_100000.mp4"
    thumb_path = temp_dir / "thumbnails" / "frontdoor_kf0001.jpg"
    thumb_path.touch()

    detections = [
        DetectionItem(
            class_id=0,
            class_name="person",
            confidence=0.88,
            timestamp_sec=1.5,
            bbox=[0.1, 0.2, 0.5, 0.8],
            keyframe_path=str(thumb_path),
        ),
        DetectionItem(
            class_id=2,
            class_name="car",
            confidence=0.75,
            timestamp_sec=3.0,
            bbox=[0.3, 0.4, 0.7, 0.9],
            keyframe_path=None,
        ),
    ]

    event = await test_db.save_event(clip_path, detections)
    assert event.id is not None
    assert event.camera_name == "frontdoor"
    assert event.thumbnail == str(thumb_path)
    assert len(event.objects) == 2

    # Fetch event by ID
    fetched = await test_db.get_event(event.id)
    assert fetched is not None
    assert fetched.id == event.id
    assert fetched.camera_name == "frontdoor"
    assert len(fetched.objects) == 2
    assert fetched.objects[0]["class"] == "person"
    assert fetched.objects[1]["class"] == "car"

    # Filter events by camera
    events_fd = await test_db.get_events(camera="frontdoor")
    assert len(events_fd) == 1
    events_other = await test_db.get_events(camera="backyard")
    assert len(events_other) == 0

    # Filter events by class
    events_person = await test_db.get_events(cls="person")
    assert len(events_person) == 1
    events_bear = await test_db.get_events(cls="bear")
    assert len(events_bear) == 0

    # Mark notified
    assert fetched.notified == 0
    await test_db.mark_notified(event.id)
    updated = await test_db.get_event(event.id)
    assert updated.notified == 1


@pytest.mark.asyncio
async def test_camera_name_derivation(test_db: Database, test_settings):
    watch_dir = test_settings.watch_dir
    processed_dir = test_settings.processed_dir

    # 1. Subdirectory structure: watch_dir/frontdoor/clip.mp4 -> frontdoor
    sub_clip = watch_dir / "frontdoor" / "clip.mp4"
    evt1 = await test_db.save_event(sub_clip, [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])])
    assert evt1.camera_name == "frontdoor"

    # 2. Serial prefix structure: watch_dir/CAM0100000001_000000e2_20260828_122954.mp4 -> CAM0100000001
    serial_clip = watch_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    evt2 = await test_db.save_event(serial_clip, [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])])
    assert evt2.camera_name == "CAM0100000001"

    # 3. Grouped processed structure: processed_dir/2026-08-28/CAM0100000001/clip.mp4 -> CAM0100000001
    grouped_clip = processed_dir / "2026-08-28" / "CAM0100000001" / "CAM0100000001_000000e2_20260828_122954-1person.mp4"
    evt3 = await test_db.save_event(grouped_clip, [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])])
    assert evt3.camera_name == "CAM0100000001"

    # 4. Flat structure without underscore: watch_dir/driveway.mp4 -> driveway
    flat_clip = watch_dir / "driveway.mp4"
    evt4 = await test_db.save_event(flat_clip, [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])])
    assert evt4.camera_name == "driveway"

    # 5. Mapped camera structure: serial resolved to name
    import json
    cfg_file = test_settings.watch_dir / "cameras_name.json"
    cfg_file.write_text(json.dumps([{"name": "Backyard", "serial": "CAM0100000001"}]))
    test_settings.cameras_config_path = cfg_file
    mapped_clip = watch_dir / "CAM0100000001_000000e2_20260828_122954.mp4"
    evt5 = await test_db.save_event(mapped_clip, [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])])
    assert evt5.camera_name == "Backyard"


@pytest.mark.asyncio
async def test_delete_event(test_db: Database, temp_dir: Path):
    clip_path = temp_dir / "clips" / "frontdoor" / "del_test.mp4"
    await test_db.mark_clip(clip_path, "done")
    evt = await test_db.save_event(clip_path, [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])])
    assert evt.id is not None

    # Verify event exists
    fetched = await test_db.get_event(evt.id)
    assert fetched is not None

    # Delete event
    res = await test_db.delete_event(evt.id)
    assert res is not None
    assert res["id"] == evt.id

    # Verify deleted from events and clips
    assert await test_db.get_event(evt.id) is None
    clips = await test_db.get_clips()
    assert not any(c.path == str(clip_path) for c in clips)

    # Deleting non-existent returns None
    assert await test_db.delete_event(99999) is None


@pytest.mark.asyncio
async def test_delete_events_batch(test_db: Database, temp_dir: Path):
    clip1 = temp_dir / "clips" / "batch1.mp4"
    clip2 = temp_dir / "clips" / "batch2.mp4"
    evt1 = await test_db.save_event(clip1, [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])])
    evt2 = await test_db.save_event(clip2, [DetectionItem(15, "cat", 0.85, 2.0, [0, 0, 1, 1])])

    deleted = await test_db.delete_events_batch([evt1.id, evt2.id, 99999])
    assert len(deleted) == 2
    deleted_ids = {d["id"] for d in deleted}
    assert deleted_ids == {evt1.id, evt2.id}

    assert await test_db.get_event(evt1.id) is None
    assert await test_db.get_event(evt2.id) is None


@pytest.mark.asyncio
async def test_prune_missing_events(test_db: Database, temp_dir: Path):
    clips_dir = temp_dir / "processed"
    clips_dir.mkdir(parents=True, exist_ok=True)

    # 1. Clip that physically exists
    clip_exists = clips_dir / "exists.mp4"
    clip_exists.write_bytes(b"dummy mp4")

    # 2. Clip that does not exist
    clip_missing = clips_dir / "missing.mp4"

    evt_exists = await test_db.save_event(clip_exists, [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])])
    evt_missing = await test_db.save_event(clip_missing, [DetectionItem(0, "car", 0.85, 2.0, [0, 0, 1, 1])])

    # Run prune
    pruned_ids = await test_db.prune_missing_events(full=True)
    assert evt_missing.id in pruned_ids
    assert evt_exists.id not in pruned_ids

    # Verify missing event is gone, existing event remains
    assert await test_db.get_event(evt_missing.id) is None
    assert await test_db.get_event(evt_exists.id) is not None


