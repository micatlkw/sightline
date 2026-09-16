from __future__ import annotations

from pathlib import Path
import pytest
from app.core.helpers import extract_camera_name, extract_date_str, format_detection_suffix
from app.models.event import DetectionItem


def test_extract_camera_name():
    # 1. Underscore prefix without mapping
    p1 = Path("/data/incoming/UNKNOWN123_000000e2_20260828_122954.mp4")
    assert extract_camera_name(p1) == "UNKNOWN123"

    # 2. Subdirectory under incoming
    p2 = Path("/data/incoming/frontdoor/clip.mp4")
    assert extract_camera_name(p2, incoming_dir=Path("/data/incoming")) == "frontdoor"

    # 3. Processed date/camera structure
    p3 = Path("/data/processed/2026-08-28/frontdoor/clip.mp4")
    assert extract_camera_name(p3, base_dirs=[Path("/data/processed")]) == "frontdoor"

    # 4. Corrupt processed structure
    p4 = Path("/data/processed/corrupt/2026-08-28/frontdoor/clip.mp4")
    assert extract_camera_name(p4, base_dirs=[Path("/data/processed")]) == "frontdoor"

    # 5. Flat fallback
    p5 = Path("/data/clips/driveway.mp4")
    assert extract_camera_name(p5) == "driveway"


def test_extract_camera_name_with_mapping(tmp_path: Path):
    # Setup test cameras_name.json
    cfg = tmp_path / "cameras_name.json"
    import json
    cfg.write_text(json.dumps([
        {"name": "Backyard", "serial": "CAM0100000001"},
        {"name": "Frontyard East", "serial": "CAM0400000004"}
    ]))

    # Mapped serials
    p1 = Path("/data/incoming/CAM0100000001_000000e2_20260828_122954.mp4")
    assert extract_camera_name(p1, cameras_config_path=cfg) == "Backyard"

    p2 = Path("/data/incoming/CAM0400000004_000000e2_20260828_122954.mp4")
    assert extract_camera_name(p2, cameras_config_path=cfg) == "Frontyard East"

    # Unmapped serial
    p3 = Path("/data/incoming/UNKNOWN999_000000e2_20260828_122954.mp4")
    assert extract_camera_name(p3, cameras_config_path=cfg) == "UNKNOWN999"


def test_camera_mapping_auto_reload(tmp_path: Path):
    from app.core.helpers import get_camera_mapping, resolve_camera_name
    import json, os, time

    cfg = tmp_path / "cameras_name.json"
    cfg.write_text(json.dumps([
        {"name": "Backyard", "serial": "CAM0100000001"}
    ]))

    m1 = get_camera_mapping(cfg)
    assert m1.get("CAM0100000001") == "Backyard"
    assert resolve_camera_name("CAM0100000001", cameras_config_path=cfg) == "Backyard"

    # Update file with new mtime
    time.sleep(0.01)
    cfg.write_text(json.dumps([
        {"name": "Backyard New", "serial": "CAM0100000001"},
        {"name": "Frontdoor", "serial": "CAM0200000002"}
    ]))
    # ensure mtime changes
    now = time.time() + 1.0
    os.utime(cfg, (now, now))

    m2 = get_camera_mapping(cfg)
    assert m2.get("CAM0100000001") == "Backyard New"
    assert m2.get("CAM0200000002") == "Frontdoor"
    assert resolve_camera_name("CAM0200000002", cameras_config_path=cfg) == "Frontdoor"


def test_get_camera_mapping_from_yaml(tmp_path: Path):
    from app.core.helpers import get_camera_mapping, extract_camera_name

    yaml_file = tmp_path / "settings.yaml"
    yaml_file.write_text("""
cameras:
  - name: "Backyard"
    serial: "CAM0100000001"
  - name: "Frontdoor"
    serial: "CAM0200000002"
""")

    mapping = get_camera_mapping(yaml_file)
    assert mapping["CAM0100000001"] == "Backyard"
    assert mapping["CAM0200000002"] == "Frontdoor"

    clip = Path("/data/incoming/CAM0100000001_000000e2_20260828_122954.mp4")
    assert extract_camera_name(clip, cameras_config_path=yaml_file) == "Backyard"



def test_extract_clip_datetime():
    from app.core.helpers import extract_clip_datetime

    p1 = Path("/data/incoming/CAM0400000004_000007ff_20260831_162758.mp4")
    dt1 = extract_clip_datetime(p1)
    assert dt1.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-31 16:27:58"

    p2 = Path("/data/incoming/CAM0100000001_000000e2_2026-08-28_122954.mp4")
    dt2 = extract_clip_datetime(p2)
    assert dt2.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-28 12:29:54"

    p3 = Path("/data/incoming/CAM0100000001_000000e2_20260828_122954-2person-1car.mp4")
    dt3 = extract_clip_datetime(p3)
    assert dt3.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-28 12:29:54"


def test_extract_date_str():
    # 1. 3rd token with 8 digits
    p1 = Path("/data/incoming/CAM0100000001_000000e2_20260828_122954.mp4")
    assert extract_date_str(p1) == "2026-08-28"

    # 2. 3rd token with formatted date YYYY-MM-DD
    p2 = Path("/data/incoming/CAM0100000001_000000e2_2026-08-28_122954.mp4")
    assert extract_date_str(p2) == "2026-08-28"

    # 3. Formatted filename with suffix
    p3 = Path("/data/incoming/CAM0100000001_000000e2_20260828_122954-2person-1car.mp4")
    assert extract_date_str(p3) == "2026-08-28"


def test_format_detection_suffix():
    # 1. No detections -> -none
    assert format_detection_suffix([]) == "-none"

    # 2. Single detection
    d1 = [DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1])]
    assert format_detection_suffix(d1) == "-1person"

    # 3. Multiple simultaneous detections in same frame
    d2 = [
        DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1]),
        DetectionItem(0, "person", 0.85, 1.0, [0, 0, 1, 1]),
        DetectionItem(2, "car", 0.8, 2.0, [0, 0, 1, 1]),
    ]
    assert format_detection_suffix(d2) == "-2person-1car"

    # 4. Same object appearing across frames vs simultaneous
    d3 = [
        DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1]),
        DetectionItem(0, "person", 0.85, 2.0, [0, 0, 1, 1]),
    ]
    assert format_detection_suffix(d3) == "-1person"

    # 5. Order of appearance
    d4 = [
        DetectionItem(2, "car", 0.8, 0.5, [0, 0, 1, 1]),
        DetectionItem(0, "person", 0.9, 1.0, [0, 0, 1, 1]),
    ]
    assert format_detection_suffix(d4) == "-1car-1person"


def test_format_local_datetime():
    from app.core.helpers import format_local_datetime

    # 1. UTC ISO string
    date_str, time_str = format_local_datetime("2026-08-30T18:30:15+00:00")
    assert date_str == "2026-08-30" or date_str == "2026-08-31"
    assert ":" in time_str
    assert "(" in time_str and ")" in time_str

    # 2. 'Z' suffix string
    d2, t2 = format_local_datetime("2026-08-30T18:30:15Z")
    assert d2 == date_str
    assert ":" in t2

    # 3. Empty or unparseable string fallback
    d3, t3 = format_local_datetime("")
    assert d3 == "" and t3 == ""

    d4, t4 = format_local_datetime("2026-08-30T12:00:00-fallback")
    assert d4 == "2026-08-30"


def test_resolve_target_classes():
    from app.core.helpers import resolve_class_id, resolve_target_classes

    assert resolve_class_id("person") == 0
    assert resolve_class_id("CAR") == 2
    assert resolve_class_id("dog") == 16
    assert resolve_class_id(16) == 16
    assert resolve_class_id("traffic_light") == 9
    assert resolve_class_id("unknown_object_xyz") is None

    resolved = resolve_target_classes(["person", 2, "dog", "bear", 999])
    assert resolved == [0, 2, 16, 21]


def test_format_duration():
    from app.core.helpers import format_duration

    # Negative clamped to 0.0s
    assert format_duration(-5.0) == "0.0s"
    assert format_duration(-0.1) == "0.0s"

    # Under 60 seconds
    assert format_duration(0.0) == "0.0s"
    assert format_duration(14.24) == "14.2s"
    assert format_duration(59.9) == "59.9s"

    # Between 60s and 3600s (minutes and seconds)
    assert format_duration(60.0) == "1m 0s"
    assert format_duration(135.0) == "2m 15s"
    assert format_duration(3599.0) == "59m 59s"

    # Over 3600s (hours and minutes)
    assert format_duration(3600.0) == "1h 0m"
    assert format_duration(7325.0) == "2h 2m"


def test_is_valid_mp4(tmp_path: Path):
    import struct
    from app.core.helpers import is_valid_mp4

    # Non-existent
    assert is_valid_mp4(tmp_path / "missing.mp4")[0] is False

    # Too small
    small = tmp_path / "small.mp4"
    small.write_bytes(b"123")
    assert is_valid_mp4(small)[0] is False

    # Valid MP4: ftyp (16) + mdat (20) + moov (16) = 52 bytes
    valid_file = tmp_path / "valid.mp4"
    valid_bytes = (
        struct.pack(">I4s", 16, b"ftyp") + b"a" * 8 +
        struct.pack(">I4s", 20, b"mdat") + b"b" * 12 +
        struct.pack(">I4s", 16, b"moov") + b"c" * 8
    )
    valid_file.write_bytes(valid_bytes)
    ok, reason = is_valid_mp4(valid_file)
    assert ok is True
    assert "Valid complete MP4" in reason

    # Unfinalized: missing moov (ftyp + mdat only)
    no_moov = tmp_path / "no_moov.mp4"
    no_moov_bytes = (
        struct.pack(">I4s", 16, b"ftyp") + b"a" * 8 +
        struct.pack(">I4s", 20, b"mdat") + b"b" * 12
    )
    no_moov.write_bytes(no_moov_bytes)
    ok, reason = is_valid_mp4(no_moov)
    assert ok is False
    assert "Missing required atoms" in reason

    # Unfinalized: mdat size == 0
    zero_mdat = tmp_path / "zero_mdat.mp4"
    zero_mdat_bytes = (
        struct.pack(">I4s", 16, b"ftyp") + b"a" * 8 +
        struct.pack(">I4s", 0, b"mdat") + b"b" * 20
    )
    zero_mdat.write_bytes(zero_mdat_bytes)
    ok, reason = is_valid_mp4(zero_mdat)
    assert ok is False
    assert "size 0" in reason


def test_get_mp4_duration(tmp_path: Path):
    import struct
    from app.core.helpers import get_mp4_duration

    # Non-existent
    assert get_mp4_duration(tmp_path / "missing.mp4") == 0.0

    # Synthetic MP4 with mvhd (version 0, timescale 1000, duration 15000 -> 15.0s)
    mvhd_payload = struct.pack(">B3sIIII", 0, b"\x00\x00\x00", 0, 0, 1000, 15000)
    mvhd_box = struct.pack(">I4s", len(mvhd_payload) + 8, b"mvhd") + mvhd_payload + (b"\x00" * 80)
    moov_box = struct.pack(">I4s", len(mvhd_box) + 8, b"moov") + mvhd_box
    ftyp_box = struct.pack(">I4s", 16, b"ftyp") + b"isom" + b"\x00\x00\x02\x00"

    mp4_file = tmp_path / "test_clip.mp4"
    mp4_file.write_bytes(ftyp_box + moov_box)

    dur = get_mp4_duration(mp4_file)
    assert abs(dur - 15.0) < 0.001



