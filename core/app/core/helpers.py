from __future__ import annotations

import json
import logging
import os
import re
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


from app.models.event import DetectionItem

import yaml

logger = logging.getLogger(__name__)

from app.config import COCO_CLASSES, COCO_NAME_TO_ID, resolve_class_id, resolve_target_classes

# Cache: config_path_str -> (mtime, mapping_dict)
_MAPPING_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def get_camera_mapping(config_path: Path | None = None) -> dict[str, str]:
    """
    Loads camera serial-to-name mapping from YAML or JSON configuration file with auto-reloading
    based on file modification time.
    Supported sources:
      1. settings.yaml (under 'cameras:' list with name and serial)
      2. settings.json (under 'cameras:' list)
      3. cameras_name.json (list of {name, serial} or dict)
    Returns a dict of uppercase_serial -> camera_name and UPPERCASE_NAME -> camera_name.
    """
    target_path = config_path
    if not target_path:
        for candidate in (
            Path("/config/settings.yaml"),
            Path("/volume1/sightline/config/settings.yaml"),
            Path("/config/settings.json"),
            Path("/volume1/sightline/config/settings.json"),
            Path("/config/cameras_name.json"),
            Path("/volume1/sightline/config/cameras_name.json"),
        ):
            if candidate.is_file():
                target_path = candidate
                break

    if not target_path or not target_path.is_file():
        return {}

    try:
        current_mtime = target_path.stat().st_mtime
    except OSError:
        return {}

    path_str = str(target_path.resolve())
    cached = _MAPPING_CACHE.get(path_str)
    if cached and cached[0] == current_mtime:
        return cached[1]

    try:
        text = target_path.read_text(encoding="utf-8")
        data = None
        if target_path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        else:
            try:
                data = json.loads(text)
            except Exception:
                data = yaml.safe_load(text)

        mapping: dict[str, str] = {}
        if isinstance(data, dict):
            if "cameras" in data and isinstance(data["cameras"], list):
                for item in data["cameras"]:
                    if isinstance(item, dict):
                        serial = item.get("serial")
                        name = item.get("name")
                        if serial and name:
                            mapping[str(serial).strip().upper()] = str(name).strip()
                            mapping[str(name).strip().upper()] = str(name).strip()
            else:
                for serial, name in data.items():
                    if serial and name and isinstance(name, str):
                        mapping[str(serial).strip().upper()] = str(name).strip()
                        mapping[str(name).strip().upper()] = str(name).strip()
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    serial = item.get("serial")
                    name = item.get("name")
                    if serial and name:
                        mapping[str(serial).strip().upper()] = str(name).strip()
                        mapping[str(name).strip().upper()] = str(name).strip()

        _MAPPING_CACHE[path_str] = (current_mtime, mapping)
        logger.info(f"[helpers] loaded {len(mapping)} camera name mapping(s) from {target_path}")
        return mapping
    except Exception as exc:
        logger.warning(f"[helpers] failed to load camera mapping from {target_path}: {exc}")
        if cached:
            return cached[1]
        return {}


def resolve_camera_name(
    raw_name_or_serial: str,
    cameras_config_path: Path | None = None,
    custom_mapping: dict[str, str] | None = None,
) -> str:
    """
    Resolves a camera serial or raw name to its mapped human-readable name.
    If no mapping exists, returns the original raw_name_or_serial.
    """
    if not raw_name_or_serial:
        return raw_name_or_serial

    mapping = (
        custom_mapping
        if custom_mapping is not None
        else get_camera_mapping(cameras_config_path)
    )
    clean_key = raw_name_or_serial.strip().upper()
    if clean_key in mapping:
        return mapping[clean_key]

    # Check if string starts with a serial prefix (e.g. CAM0100000001_000007ff_20260831_162758)
    if "_" in clean_key:
        prefix = clean_key.split("_")[0].strip()
        if prefix in mapping:
            return mapping[prefix]

    return raw_name_or_serial


def extract_camera_name(
    clip_path: Path,
    incoming_dir: Optional[Path] = None,
    base_dirs: Optional[list[Path]] = None,
    cameras_config_path: Optional[Path] = None,
    custom_mapping: Optional[dict[str, str]] = None,
) -> str:
    """
    Extracts the camera name / serial.
    Priority:
      1. Filename prefix before the first underscore (e.g. CAM0100000001_000000e2_... -> CAM0100000001).
      2. Subdirectory name under base directories (e.g. incoming_dir, processed_dir, watch_dir),
         skipping 'corrupt' and date folders (YYYY-MM-DD).
      3. Parent directory name if not a generic directory (incoming, processed, clips, corrupt, date).
      4. Fallback to clip_path.stem.
    Then maps serial to human-readable camera name if a mapping is found.
    """
    raw_name = ""
    stem = clip_path.stem
    if "_" in stem:
        camera = stem.split("_")[0].strip()
        if camera:
            raw_name = camera

    if not raw_name:
        all_bases: list[Path] = []
        if base_dirs:
            all_bases.extend(base_dirs)
        if incoming_dir and incoming_dir not in all_bases:
            all_bases.append(incoming_dir)

        for base in all_bases:
            try:
                rel = clip_path.resolve().relative_to(base.resolve())
                parts = list(rel.parts[:-1])  # Exclude filename
                if not parts:
                    continue
                if parts[0] == "corrupt":
                    parts.pop(0)
                if parts and re.match(r"^\d{4}-\d{2}-\d{2}$", parts[0]):
                    parts.pop(0)
                if parts:
                    raw_name = parts[0]
                    break
            except (ValueError, Exception):
                continue

    if not raw_name:
        parent_name = clip_path.parent.name
        if (
            parent_name
            and parent_name not in ("incoming", "processed", "clips", "corrupt")
            and not re.match(r"^\d{4}-\d{2}-\d{2}$", parent_name)
        ):
            raw_name = parent_name

    if not raw_name:
        raw_name = stem

    return resolve_camera_name(
        raw_name,
        cameras_config_path=cameras_config_path,
        custom_mapping=custom_mapping,
    )


def extract_clip_datetime(clip_path: Path) -> datetime:
    """
    Extracts datetime from video clip filename or file mtime.
    Supports formats:
      - <serial>_<seq>_<YYYYMMDD>_<HHMMSS> (e.g. CAM0100000001_000007ff_20260831_162758.mp4)
      - <serial>_<seq>_<YYYY-MM-DD>_<HHMMSS> (e.g. CAM0100000001_000000e2_2026-08-28_122954.mp4)
      - YYYY-MM-DD_HH-MM-SS or YYYYMMDD_HHMMSS or ISO timestamp in filename
      - Fallback: file mtime or current datetime
    Returns a timezone-aware datetime in local system timezone.
    """
    stem = clip_path.stem
    # 1. Look for YYYYMMDD_HHMMSS or YYYY-MM-DD_HHMMSS or YYYY-MM-DD_HH-MM-SS or YYYYMMDDTHHMMSS
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[T_](\d{2})[-_:]?(\d{2})[-_:]?(\d{2})", stem)
    if m:
        try:
            year, month, day, hour, minute, second = map(int, m.groups())
            return datetime(year, month, day, hour, minute, second).astimezone()
        except ValueError:
            pass

    # 2. Look for compact YYYYMMDDHHMMSS
    m = re.search(r"(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", stem)
    if m:
        try:
            year, month, day, hour, minute, second = map(int, m.groups())
            return datetime(year, month, day, hour, minute, second).astimezone()
        except ValueError:
            pass

    # 3. Fallback to file mtime if file exists
    try:
        if clip_path.is_file():
            mtime = clip_path.stat().st_mtime
            return datetime.fromtimestamp(mtime).astimezone()
    except Exception:
        pass

    # 4. Fallback to now
    return datetime.now().astimezone()


def extract_date_str(clip_path: Path) -> str:
    """
    Extracts date in YYYY-MM-DD format.
    """
    return extract_clip_datetime(clip_path).strftime("%Y-%m-%d")


def format_detection_suffix(detections: list[DetectionItem]) -> str:
    """
    Builds the filename suffix based on detected objects.
    - If no detections: returns '-none'.
    - If detections: calculates the maximum simultaneous count of each class across
      all keyframes, preserves order of first appearance, and prefixes all counts
      (e.g. '-2person-1car', '-1person').
    """
    if not detections:
        return "-none"

    first_seen: dict[str, float] = {}
    frame_counts: dict[float, dict[str, int]] = {}

    for d in detections:
        cls_name = d.class_name.lower().replace(" ", "_")
        if cls_name not in first_seen:
            first_seen[cls_name] = d.timestamp_sec

        frame_dict = frame_counts.setdefault(d.timestamp_sec, {})
        frame_dict[cls_name] = frame_dict.get(cls_name, 0) + 1

    max_counts: dict[str, int] = {}
    for frame_dict in frame_counts.values():
        for cls_name, count in frame_dict.items():
            if count > max_counts.get(cls_name, 0):
                max_counts[cls_name] = count

    ordered_classes = sorted(max_counts.keys(), key=lambda c: (first_seen.get(c, 0.0), c))
    tokens = [f"{max_counts[c]}{c}" for c in ordered_classes]
    return "-" + "-".join(tokens)


def init_system_timezone() -> None:
    """Initializes system timezone from Synology /etc/TZ if TZ env var is not set."""
    if "TZ" not in os.environ:
        for tz_file in (Path("/etc/TZ"), Path("/etc/timezone")):
            if tz_file.is_file():
                try:
                    val = tz_file.read_text().strip()
                    if val:
                        os.environ["TZ"] = val
                        time.tzset()
                        logger.info(f"[helpers] initialized timezone from {tz_file}: {val}")
                        break
                except Exception as exc:
                    logger.debug(f"[helpers] could not read timezone from {tz_file}: {exc}")


init_system_timezone()



def format_local_datetime(iso_str: str) -> tuple[str, str]:
    """
    Converts an ISO-8601 timestamp string into human-readable local date and time strings.
    Returns: (date_str: 'YYYY-MM-DD', time_str: 'HH:MM:SS (TZ)')
    Example: ('2026-08-30', '11:53:15 (PDT)')
    """
    if not iso_str:
        return "", ""

    try:
        # Handle 'Z' or offset ISO strings
        clean_iso = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        # Convert to local timezone
        local_dt = dt.astimezone()
        date_str = local_dt.strftime("%Y-%m-%d")
        tz_name = local_dt.strftime("%Z") or local_dt.tzname() or ""
        time_part = local_dt.strftime("%H:%M:%S")
        time_str = f"{time_part} ({tz_name})" if tz_name else time_part
        return date_str, time_str
    except Exception:
        # Fallback if unparseable
        if "T" in iso_str:
            parts = iso_str.split("T")
            return parts[0], parts[1][:8]
        return iso_str[:10], iso_str[11:19]


def format_detected_objects_summary(objects: list[Any] | None) -> str:
    """
    Deduplicates detected objects by class name, finds the maximum confidence
    for each class, sorts unique classes descending by max confidence,
    and returns a clean, capitalized comma-separated string (e.g. 'Person, Car, Dog').
    """
    if not objects:
        return "Motion"

    max_conf_by_class: dict[str, float] = {}
    for obj in objects:
        if isinstance(obj, dict):
            raw_cls = str(obj.get("class") or obj.get("class_name") or "").strip()
            conf_val = obj.get("confidence", 0.0)
        elif hasattr(obj, "class_name"):
            raw_cls = str(getattr(obj, "class_name", "")).strip()
            conf_val = getattr(obj, "confidence", 0.0)
        else:
            continue

        if not raw_cls:
            continue

        try:
            conf = float(conf_val or 0.0)
        except (ValueError, TypeError):
            conf = 0.0

        if raw_cls not in max_conf_by_class or conf > max_conf_by_class[raw_cls]:
            max_conf_by_class[raw_cls] = conf

    if not max_conf_by_class:
        return "Motion"

    # Sort descending by maximum confidence
    sorted_classes = sorted(
        max_conf_by_class.keys(),
        key=lambda c: max_conf_by_class[c],
        reverse=True,
    )

    return ", ".join(c.capitalize() for c in sorted_classes)


def format_duration(seconds: float) -> str:
    """
    Formats a duration in seconds into a human-readable string.
    - Negative values (e.g. clock drift) are clamped to '0.0s'.
    - Under 60s: formatted in seconds with 1 decimal place (e.g. '14.2s').
    - 60s to 3600s: formatted in minutes and seconds (e.g. '2m 15s').
    - >= 3600s: formatted in hours and minutes (e.g. '2h 15m').
    """
    if seconds < 0:
        return "0.0s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {int(secs)}s"
    hours, mins = divmod(minutes, 60)
    return f"{int(hours)}h {int(mins)}m"


def is_valid_mp4(filepath: Path | str) -> tuple[bool, str]:
    """
    Validates that an MP4 file is structurally complete and finalized:
    - Minimum valid MP4 container size (>= 32 bytes).
    - Top-level atom sequence starts with 'ftyp' box.
    - All atom sizes sum up exactly to the total file size (no trailing incomplete data).
    - 'mdat' atom has a non-zero, explicit size (during streaming, mdat size is 0).
    - 'moov' atom is present at the end (contains index tables and track metadata).
    - Strict bounds checking and loop ceiling to prevent DoS or hangs.
    """
    try:
        p = Path(filepath)
        if not p.is_file():
            return False, "File does not exist"
        filesize = p.stat().st_size
        if filesize < 32:
            return False, f"File too small ({filesize} bytes)"

        with open(p, "rb") as f:
            has_ftyp = False
            has_moov = False
            has_mdat = False
            pos = 0
            atom_count = 0
            max_atoms = 10000

            while pos < filesize:
                atom_count += 1
                if atom_count > max_atoms:
                    return False, f"Excessive atom count ({atom_count}) exceeded safe limit"

                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    return False, f"Truncated atom header at offset {pos}"

                size, tag = struct.unpack(">I4s", header)
                try:
                    tag_str = tag.decode("latin1", errors="replace")
                except Exception:
                    tag_str = "????"

                if size == 1:
                    ext_header = f.read(8)
                    if len(ext_header) < 8:
                        return False, f"Truncated 64-bit size at offset {pos}"
                    size = struct.unpack(">Q", ext_header)[0]
                elif size == 0:
                    return False, f"Unfinalized atom {tag_str} (size 0 extends to EOF) at offset {pos}"
                elif size < 8:
                    return False, f"Invalid atom size ({size} bytes) for {tag_str} at offset {pos}"

                if pos + size > filesize:
                    return False, f"Atom {tag_str} extends beyond file ({pos + size} > {filesize})"

                if tag_str == "ftyp":
                    has_ftyp = True
                elif tag_str == "moov":
                    has_moov = True
                elif tag_str == "mdat":
                    has_mdat = True

                pos += size

            if pos != filesize:
                return False, f"Atom size sum ({pos}) != file size ({filesize})"

            if not (has_ftyp and has_mdat and has_moov):
                return False, f"Missing required atoms (ftyp={has_ftyp}, mdat={has_mdat}, moov={has_moov})"

            return True, "Valid complete MP4"
    except Exception as e:
        return False, f"Validation error: {e}"


def get_mp4_duration(filepath: Path | str) -> float:
    """
    Extracts video duration in seconds from the MP4 mvhd (Movie Header) atom.
    Runs in < 1ms without spawning ffprobe or decoding video frames.
    """
    try:
        p = Path(filepath)
        if not p.is_file():
            return 0.0
        filesize = p.stat().st_size
        with open(p, "rb") as f:
            pos = 0
            while pos < filesize:
                f.seek(pos)
                h = f.read(8)
                if len(h) < 8:
                    break
                size, tag = struct.unpack(">I4s", h)
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                elif size == 0:
                    size = filesize - pos

                if tag == b"moov":
                    moov_data = f.read(min(size - 8, 4096))
                    mvhd_idx = moov_data.find(b"mvhd")
                    if mvhd_idx >= 4:
                        mvhd_box = moov_data[mvhd_idx - 4:]
                        if len(mvhd_box) >= 28:
                            _, _, ver = struct.unpack(">I4sB", mvhd_box[:9])
                            if ver == 0:
                                _, _, ts, dur = struct.unpack(">IIII", mvhd_box[12:28])
                                return (dur / ts) if ts > 0 else 0.0
                            elif ver == 1 and len(mvhd_box) >= 44:
                                _, _, ts, dur = struct.unpack(">QQIQ", mvhd_box[12:44])
                                return (dur / ts) if ts > 0 else 0.0
                    return 0.0
                pos += size
    except Exception as exc:
        logger.debug(f"[helpers] failed to parse duration for {filepath}: {exc}")
    return 0.0






