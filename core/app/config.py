from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import yaml

logger = logging.getLogger(__name__)

# Permitted Apprise notification schemes (rejects local file://, script execution, etc.)
ALLOWED_APPRISE_SCHEMES: frozenset[str] = frozenset({
    # Web & Push
    "http", "https", "ntfy", "ntfys", "pover", "pushbullet", "gotify", "bark", "onepush",
    # Chat & Collaboration
    "tgram", "discord", "slack", "matrix", "mattermost", "teams", "rocket", "zulip",
    # Email
    "mailto", "mailgun", "sendgrid", "ses",
    # SMS & Pager
    "twilio", "pagerduty", "opsgenie",
    # Generic webhooks
    "json", "xml", "form",
})

# Standard 80 COCO classes mapping ID -> Name
COCO_CLASSES: dict[int, str] = {
    0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 4: "airplane", 5: "bus",
    6: "train", 7: "truck", 8: "boat", 9: "traffic light", 10: "fire hydrant",
    11: "stop sign", 12: "parking meter", 13: "bench", 14: "bird", 15: "cat",
    16: "dog", 17: "horse", 18: "sheep", 19: "cow", 20: "elephant", 21: "bear",
    22: "zebra", 23: "giraffe", 24: "backpack", 25: "umbrella", 26: "handbag",
    27: "tie", 28: "suitcase", 29: "frisbee", 30: "skis", 31: "snowboard",
    32: "sports ball", 33: "kite", 34: "baseball bat", 35: "baseball glove",
    36: "skateboard", 37: "surfboard", 38: "tennis racket", 39: "bottle",
    40: "wine glass", 41: "cup", 42: "fork", 43: "knife", 44: "spoon", 45: "bowl",
    46: "banana", 47: "apple", 48: "sandwich", 49: "orange", 50: "broccoli",
    51: "carrot", 52: "hot dog", 53: "pizza", 54: "donut", 55: "cake",
    56: "chair", 57: "couch", 58: "potted plant", 59: "bed", 60: "dining table",
    61: "toilet", 62: "tv", 63: "laptop", 64: "mouse", 65: "remote",
    66: "keyboard", 67: "cell phone", 68: "microwave", 69: "oven", 70: "toaster",
    71: "sink", 72: "refrigerator", 73: "book", 74: "clock", 75: "vase",
    76: "scissors", 77: "teddy bear", 78: "hair drier", 79: "toothbrush",
}

COCO_NAME_TO_ID: dict[str, int] = {}
for _cid, _cname in COCO_CLASSES.items():
    COCO_NAME_TO_ID[_cname.lower()] = _cid
    COCO_NAME_TO_ID[_cname.lower().replace(" ", "_")] = _cid


def resolve_class_id(val: int | str) -> int | None:
    """Converts a class name or ID into its standard COCO integer ID."""
    if isinstance(val, int):
        return val if 0 <= val <= 79 else None
    if isinstance(val, str):
        val_clean = val.strip().lower()
        if val_clean.isdigit():
            return int(val_clean)
        return COCO_NAME_TO_ID.get(val_clean) or COCO_NAME_TO_ID.get(val_clean.replace(" ", "_"))
    return None


def resolve_target_classes(classes: list[int | str] | None) -> list[int]:
    """Resolves a mixed list of class names and IDs into sorted integer class IDs."""
    if not classes:
        return []
    resolved = set()
    for c in classes:
        cid = resolve_class_id(c)
        if cid is not None:
            resolved.add(cid)
        else:
            logger.warning(f"[config] unrecognized COCO class name or ID: {c}")
    return sorted(resolved)


def normalize_class_confidence_thresholds(val: Any) -> dict[str, float] | None:
    """
    Normalizes a dictionary of {class_name_or_id: threshold} into {coco_class_name: float}.
    - Validates class name/id against COCO classes.
    - Validates threshold (0.01 <= t <= 1.0 or 1% <= t <= 100%).
    """
    if val is None or val == "":
        return None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return None
    if not isinstance(val, dict):
        raise ValueError("class_confidence_thresholds must be a dictionary of class names to thresholds")

    normalized: dict[str, float] = {}
    for k, v in val.items():
        if v is None or v == "":
            continue
        cid = resolve_class_id(k)
        if cid is None or cid not in COCO_CLASSES:
            logger.warning(f"[config] unrecognized COCO class name or ID in class_confidence_thresholds: {k}")
            continue
        cname = COCO_CLASSES[cid]
        try:
            t = float(v)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid confidence threshold for class '{k}': {v}")
        if t > 1.0:
            t = t / 100.0
        if not (0.01 <= t <= 1.0):
            raise ValueError(f"Confidence threshold for '{k}' must be between 0.01 and 1.0 (or 1% and 100%)")
        normalized[cname] = round(t, 4)

    return normalized


def _fallback_get_camera_mapping(path: Path | None) -> dict[str, str]:
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mapping: dict[str, str] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    s = item.get("serial")
                    n = item.get("name")
                    if s and n:
                        mapping[str(s).strip().upper()] = str(n).strip()
        elif isinstance(data, dict):
            for s, n in data.items():
                if s and n:
                    mapping[str(s).strip().upper()] = str(n).strip()
        return mapping
    except Exception:
        return {}


class CameraConfig(BaseModel):
    name: str
    serial: str
    enabled: bool = True
    target_classes: list[Any] | None = None
    confidence_threshold: float | None = None
    class_confidence_thresholds: dict[str, float] | None = None
    sample_fps: float | None = Field(None, ge=0.1, le=30.0, description="Keyframes sampled per second of video")
    cooldown_seconds: int | None = None

    @field_validator("name")
    @classmethod
    def _validate_camera_name(cls, v: str) -> str:
        s = str(v).strip()
        if not s:
            raise ValueError("Camera name cannot be empty")
        if re.search(r"[\r\n\"'{};`\\]", s) or ".." in s or "/" in s:
            raise ValueError(
                "Camera name must not contain newlines, quotes, braces, path traversal ('..'), or slashes ('/')"
            )
        return s

    @field_validator("serial")
    @classmethod
    def _validate_camera_serial(cls, v: str) -> str:
        s = str(v).strip()
        if not s:
            raise ValueError("Camera serial cannot be empty")
        if re.search(r"[\r\n\"'{};`\\]", s) or ".." in s or "/" in s:
            raise ValueError(
                "Camera serial must not contain newlines, quotes, braces, path traversal ('..'), or slashes ('/')"
            )
        return s

    @field_validator("confidence_threshold", mode="before")
    @classmethod
    def _validate_confidence_threshold(cls, v: Any) -> float | None:
        if v is None or v == "":
            return None
        val = float(v)
        if val > 1.0:
            val = val / 100.0
        if not (0.01 <= val <= 1.0):
            raise ValueError("Confidence threshold must be between 0.01 and 1.0 (or 1% and 100%)")
        return round(val, 4)

    @field_validator("class_confidence_thresholds", mode="before")
    @classmethod
    def _validate_class_confidence_thresholds(cls, v: Any) -> dict[str, float] | None:
        return normalize_class_confidence_thresholds(v)



class Settings(BaseSettings):
    """
    All configuration is driven by YAML/JSON configuration files or environment variables.
    Priority: Hardcoded Defaults -> .env / Environment Variables -> settings.yaml / settings.json.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Paths ──────────────────────────────────────────────────────────────────
    incoming_dir: Path = Path("/data/incoming")
    processed_dir: Path = Path("/data/processed")
    watch_dir: Path | None = None
    db_path: Path = Path("/data/sightline.db")
    models_dir: Path = Path("/models")
    thumbnails_dir: Path = Path("/data/thumbnails")
    cameras_config_path: Path = Path("/config/cameras_name.json")

    settings_config_path: Path = Path("/config/settings.yaml")

    def model_post_init(self, __context: Any) -> None:
        if self.watch_dir is not None and self.incoming_dir == Path("/data/incoming"):
            self.incoming_dir = self.watch_dir
        self.watch_dir = self.incoming_dir

        if not self.cameras_config_path.exists():
            host_fallback = Path("/volume1/sightline/config/cameras_name.json")
            if host_fallback.exists():
                self.cameras_config_path = host_fallback

        # Resolve settings config path (prefer .yaml, fallback to .json or host paths)
        candidate_paths = [
            self.settings_config_path,
            Path("/config/settings.yaml"),
            Path("/config/settings.json"),
            Path("/volume1/sightline/config/settings.yaml"),
            Path("/volume1/sightline/config/settings.json"),
        ]
        for candidate in candidate_paths:
            if candidate.is_file():
                self.settings_config_path = candidate
                break
        else:
            host_yaml = Path("/volume1/sightline/config/settings.yaml")
            if host_yaml.parent.exists():
                self.settings_config_path = host_yaml

        # Resolve caddyfile path alongside settings config path
        self.caddyfile_path = self.settings_config_path.parent / "Caddyfile"

        # Synchronize sample_fps and vid_stride
        if self.sample_fps and self.sample_fps >= 0.1:
            self.vid_stride = max(1, round(30.0 / self.sample_fps))
        elif self.vid_stride and self.vid_stride >= 1:
            self.sample_fps = round(30.0 / self.vid_stride, 2)

        # Auto-enable tunnel mode if TUNNEL_MODE=true or TUNNEL_TOKEN is set in environment
        if not self.tunnel_mode:
            env_mode = os.environ.get("TUNNEL_MODE", "").strip().lower()
            if env_mode in ("true", "1", "yes") or bool(os.environ.get("TUNNEL_TOKEN", "").strip()):
                self.tunnel_mode = True

    # ── HTTPS & Reverse Proxy ──────────────────────────────────────────────────
    domain_name: str | None = None
    acme_email: str | None = None
    https_port: int = 8443
    http_port: int = 8080
    ssl_cert_path: Path | None = None
    ssl_key_path: Path | None = None
    caddyfile_path: Path = Path("/config/Caddyfile")
    lan_hosts: list[str] = []
    tunnel_mode: bool = False

    # ── Detection ──────────────────────────────────────────────────────────────
    yolo_model: str = "yolo11n.pt"
    yolo_model_sha256: str | None = None
    # COCO IDs / names: person(0), car(2), motorcycle(3), truck(7), cat(15), dog(16), bear(21)
    target_classes: list[Any] = [0, 2, 3, 7, 15, 16, 21]
    confidence_threshold: float = 0.45
    class_confidence_thresholds: dict[str, float] = Field(default_factory=dict)
    # Keyframe sampling rate in FPS (e.g. 1.0 = 1 frame/sec, 2.0 = 2 frames/sec, 0.5 = 1 frame every 2s)
    sample_fps: float = Field(1.0, ge=0.1, le=30.0)
    # Minimum keyframes evaluated per clip to guarantee detection density on short clips
    min_clip_keyframes: int = Field(5, ge=0, le=100)
    # Frame interval (legacy compatibility, kept in sync with sample_fps): 30 → 1 keyframe per second at 30 fps
    vid_stride: int = 30

    @model_validator(mode="before")
    @classmethod
    def _migrate_sample_fps_and_stride(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "vid_stride" in data and ("sample_fps" not in data or data["sample_fps"] is None):
                try:
                    stride = int(data["vid_stride"])
                    if stride >= 1:
                        data["sample_fps"] = round(30.0 / stride, 2)
                except Exception:
                    pass
            elif "sample_fps" in data and data["sample_fps"] is not None and ("vid_stride" not in data or data["vid_stride"] is None):
                try:
                    fps = float(data["sample_fps"])
                    if fps >= 0.1:
                        data["vid_stride"] = max(1, round(30.0 / fps))
                except Exception:
                    pass
        return data

    # ── Cameras ────────────────────────────────────────────────────────────────
    cameras: list[CameraConfig] = []

    # ── Watcher ────────────────────────────────────────────────────────────────
    # false = InotifyObserver (use when running on the NAS — local filesystem)
    # true  = PollingObserver  (use when watch_dir is an NFS/SMB network mount)
    watch_use_polling: bool = False
    # Seconds of stable (unchanging) file size before a clip is queued
    clip_stable_seconds: float = 2.0
    # Scan incoming directory for unprocessed clips on startup
    scan_on_startup: bool = True
    # Pipeline execution concurrency (number of concurrent clip processing workers)
    pipeline_concurrency: int = 2

    # ── API & Web UI ───────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    ui_host: str = "0.0.0.0"
    ui_port: int = 8080
    enable_api_docs: bool = False

    # ── Authentication (Google SSO) ────────────────────────────────────────────
    # JSON list of allowed emails, e.g. ["alice@gmail.com", "bob@gmail.com"]
    # If empty, authentication is bypassed (for local development/testing).
    allowed_google_emails: list[str] = []
    # Dedicated admin emails permitted to modify camera settings over WAN
    admin_google_emails: list[str] = []
    # Optional Google OAuth client ID to verify audience against
    google_client_id: str | None = None
    # Allow requests from local LAN subnets (192.168.x.x, 10.x.x.x, etc.) to bypass Google SSO
    allow_lan_auth_bypass: bool = True

    # ── Notifications ──────────────────────────────────────────────────────────
    # JSON list of Apprise URLs, e.g.: ["ntfy://ntfy.sh/topic", "pover://t/k"]
    apprise_urls: list[str] = []
    # Minimum seconds between alerts per camera (suppresses alert storms)
    alert_cooldown_seconds: int = 60
    # Path to Firebase service account JSON for FCM push notifications
    firebase_credentials_path: Path | None = Path("/models/firebase_credentials.json")
    # Web Push (PWA) RFC 8030 Topic header mode for collapsing burst notifications
    # Options: "camera" (per-camera collapsing), "global" (single system-wide queue), "disabled"
    webpush_topic_mode: str = "camera"
    # Web Push time-to-live in seconds (default 86400 = 24 hours)
    webpush_ttl_seconds: int = 86400

    # ── Validators ─────────────────────────────────────────────────────────────
    @field_validator("confidence_threshold", mode="before")
    @classmethod
    def _validate_global_confidence_threshold(cls, v: Any) -> float:
        if v is None or v == "":
            return 0.45
        val = float(v)
        if val > 1.0:
            val = val / 100.0
        if not (0.01 <= val <= 1.0):
            raise ValueError("Confidence threshold must be between 0.01 and 1.0 (or 1% and 100%)")
        return round(val, 4)

    @field_validator("class_confidence_thresholds", mode="before")
    @classmethod
    def _validate_global_class_confidence_thresholds(cls, v: Any) -> dict[str, float]:
        norm = normalize_class_confidence_thresholds(v)
        return norm if norm is not None else {}


    @field_validator("target_classes", "apprise_urls", "allowed_google_emails", "admin_google_emails", "lan_hosts", mode="before")
    @classmethod
    def _parse_json_list(cls, v: Any) -> Any:
        """Allow env vars like TARGET_CLASSES=[0,2,16] or ALLOWED_GOOGLE_EMAILS=["a@b.com"] (JSON string)."""
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                pass
        return v

    @field_validator("allowed_google_emails", "admin_google_emails")
    @classmethod
    def _lowercase_emails(cls, v: list[str]) -> list[str]:
        return [x.strip().lower() for x in v if isinstance(x, str)]

    @field_validator("apprise_urls")
    @classmethod
    def _validate_and_strip_urls(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for x in v:
            if not isinstance(x, str):
                continue
            url = x.strip()
            if not url:
                continue
            parsed = urllib.parse.urlparse(url)
            scheme = parsed.scheme.lower()
            if not scheme:
                raise ValueError(f"Notification URL missing scheme: {url[:50]}")
            if scheme not in ALLOWED_APPRISE_SCHEMES:
                raise ValueError(
                    f"Notification scheme '{scheme}' is not permitted (must be one of: "
                    f"{', '.join(sorted(ALLOWED_APPRISE_SCHEMES))})"
                )
            cleaned.append(url)
        return cleaned

    @field_validator("domain_name")
    @classmethod
    def _validate_domain_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if re.search(r"[\r\n\s{};\"'\\]", v):
            raise ValueError("domain_name must not contain newlines, spaces, or control characters")
        return v

    @field_validator("acme_email")
    @classmethod
    def _validate_acme_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if re.search(r"[\r\n\s{};\"'\\]", v) or "@" not in v:
            raise ValueError("acme_email must be a valid email and must not contain newlines or control characters")
        return v.lower()

    @field_validator("ssl_cert_path", "ssl_key_path")
    @classmethod
    def _validate_ssl_paths(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        s = str(v).strip()
        if re.search(r"[\r\n{};\"']", s):
            raise ValueError("SSL certificate/key path must not contain newlines or control characters")
        return Path(s)

    @field_validator("webpush_topic_mode", mode="before")
    @classmethod
    def _validate_webpush_topic_mode(cls, v: Any) -> str:
        if v is None or v == "":
            return "camera"
        val = str(v).strip().lower()
        if val in ("camera", "cam", "per_camera", "per-camera"):
            return "camera"
        if val in ("global", "all"):
            return "global"
        if val in ("disabled", "none", "off", "false"):
            return "disabled"
        raise ValueError("webpush_topic_mode must be one of: 'camera', 'global', or 'disabled'")

    @field_validator("webpush_ttl_seconds", mode="before")
    @classmethod
    def _validate_webpush_ttl_seconds(cls, v: Any) -> int:
        if v is None or v == "":
            return 86400
        try:
            val = int(v)
        except (ValueError, TypeError):
            raise ValueError("webpush_ttl_seconds must be an integer")
        if val < 0:
            raise ValueError("webpush_ttl_seconds must be non-negative")
        return val

    # ── Camera & Target Class Resolvers ─────────────────────────────────────────
    def get_resolved_target_classes(self) -> list[int]:
        """Returns the global target classes resolved to integer COCO IDs."""
        return resolve_target_classes(self.target_classes)

    def get_camera_mapping(self) -> dict[str, str]:
        """Returns mapping of uppercase serial -> camera name, falling back to cameras_name.json if needed."""
        mapping: dict[str, str] = {}
        if self.cameras:
            for cam in self.cameras:
                if isinstance(cam, dict):
                    s = str(cam.get("serial", "")).strip().upper()
                    n = str(cam.get("name", "")).strip()
                    if s and n:
                        mapping[s] = n
                        mapping[n.upper()] = n
                elif hasattr(cam, "serial") and hasattr(cam, "name"):
                    mapping[cam.serial.strip().upper()] = cam.name.strip()
                    mapping[cam.name.strip().upper()] = cam.name.strip()
            return mapping

        # Fallback to cameras_name.json if cameras list is empty
        return _fallback_get_camera_mapping(self.cameras_config_path)

    def get_camera_config(self, camera_name_or_serial: str) -> CameraConfig | None:
        """Finds CameraConfig for a given camera name or serial."""
        if not camera_name_or_serial:
            return None
        key = camera_name_or_serial.strip().upper()
        for cam in self.cameras:
            if isinstance(cam, dict):
                s = str(cam.get("serial", "")).strip().upper()
                n = str(cam.get("name", "")).strip().upper()
                if s == key or n == key:
                    return CameraConfig(**cam)
            elif hasattr(cam, "serial") and hasattr(cam, "name"):
                if cam.serial.strip().upper() == key or cam.name.strip().upper() == key:
                    return cam
        return None

    def is_camera_enabled(self, camera_name_or_serial: str) -> bool:
        """Checks if detection is enabled for the given camera (defaults to True)."""
        cam = self.get_camera_config(camera_name_or_serial)
        return cam.enabled if cam is not None else True

    def get_camera_target_classes(self, camera_name_or_serial: str) -> list[int]:
        """Returns resolved integer class IDs for camera, or global target classes if not specified."""
        cam = self.get_camera_config(camera_name_or_serial)
        if cam is not None and cam.target_classes is not None:
            return resolve_target_classes(cam.target_classes)
        return self.get_resolved_target_classes()

    def get_camera_confidence_threshold(self, camera_name_or_serial: str) -> float:
        """Returns per-camera confidence threshold override or global threshold."""
        cam = self.get_camera_config(camera_name_or_serial)
        if cam is not None and cam.confidence_threshold is not None:
            return cam.confidence_threshold
        return self.confidence_threshold

    def get_camera_class_confidence_thresholds(self, camera_name_or_serial: str) -> dict[int, float]:
        """
        Returns a dictionary of integer COCO class ID -> float confidence threshold
        by merging global class_confidence_thresholds with camera-level overrides.
        Camera-level overrides take precedence over global class thresholds.
        """
        merged: dict[int, float] = {}

        # 1. Start with global class_confidence_thresholds
        if self.class_confidence_thresholds:
            for cls_key, thresh in self.class_confidence_thresholds.items():
                cid = resolve_class_id(cls_key)
                if cid is not None:
                    merged[cid] = thresh

        # 2. Apply camera-specific overrides
        cam = self.get_camera_config(camera_name_or_serial)
        if cam is not None and cam.class_confidence_thresholds:
            for cls_key, thresh in cam.class_confidence_thresholds.items():
                cid = resolve_class_id(cls_key)
                if cid is not None:
                    merged[cid] = thresh

        return merged

    def get_camera_cooldown_seconds(self, camera_name_or_serial: str) -> int:

        """Returns per-camera cooldown override or global cooldown."""
        cam = self.get_camera_config(camera_name_or_serial)
        if cam is not None and cam.cooldown_seconds is not None:
            return cam.cooldown_seconds
        return self.alert_cooldown_seconds

    def get_camera_sample_fps(self, camera_name_or_serial: str) -> float:
        """Returns per-camera sample_fps override or global default."""
        cam = self.get_camera_config(camera_name_or_serial)
        if cam is not None and cam.sample_fps is not None and cam.sample_fps >= 0.1:
            return cam.sample_fps
        return self.sample_fps

    def resolve_clip_stride_and_fps(
        self,
        camera_name_or_serial: str,
        clip_fps: float = 30.0,
        total_frames: int = 0,
    ) -> tuple[int, float, bool]:
        """
        Calculates (vid_stride, effective_fps, is_adapted) for a clip.
        - target_fps: camera override or global sample_fps
        - effective_fps: dynamically increased if duration * target_fps < min_clip_keyframes
        - vid_stride: max(1, round(clip_fps / effective_fps))
        """
        target_fps = self.get_camera_sample_fps(camera_name_or_serial)
        fps = clip_fps if clip_fps > 0 else 30.0
        duration_sec = (total_frames / fps) if (fps > 0 and total_frames > 0) else 0.0
        effective_fps = target_fps
        is_adapted = False

        if self.min_clip_keyframes > 1 and duration_sec > 0:
            if (duration_sec * target_fps) < self.min_clip_keyframes:
                effective_fps = max(target_fps, self.min_clip_keyframes / duration_sec)
                is_adapted = True

        vid_stride = max(1, round(fps / effective_fps)) if effective_fps > 0 else 30
        return vid_stride, effective_fps, is_adapted

    # ── Export & Persistence ───────────────────────────────────────────────────
    def export_editable_dict(self) -> dict[str, Any]:
        """Returns dictionary of active settings formatted for serialization."""
        return {
            "yolo_model": self.yolo_model,
            "yolo_model_sha256": self.yolo_model_sha256,
            "target_classes": self.target_classes,
            "confidence_threshold": self.confidence_threshold,
            "class_confidence_thresholds": self.class_confidence_thresholds,
            "sample_fps": self.sample_fps,
            "min_clip_keyframes": self.min_clip_keyframes,
            "vid_stride": self.vid_stride,
            "cameras": [
                c.model_dump() if hasattr(c, "model_dump")
                else (c.dict() if hasattr(c, "dict") else dict(c))
                for c in self.cameras
            ],
            "alert_cooldown_seconds": self.alert_cooldown_seconds,
            "apprise_urls": self.apprise_urls,
            "clip_stable_seconds": self.clip_stable_seconds,
            "scan_on_startup": self.scan_on_startup,
            "pipeline_concurrency": self.pipeline_concurrency,
            "watch_use_polling": self.watch_use_polling,
            "api_host": self.api_host,
            "api_port": self.api_port,
            "ui_host": self.ui_host,
            "ui_port": self.ui_port,
            "enable_api_docs": self.enable_api_docs,
            "domain_name": self.domain_name,
            "acme_email": self.acme_email,
            "https_port": self.https_port,
            "http_port": self.http_port,
            "ssl_cert_path": str(self.ssl_cert_path) if self.ssl_cert_path else None,
            "ssl_key_path": str(self.ssl_key_path) if self.ssl_key_path else None,
            "allowed_google_emails": self.allowed_google_emails,
            "admin_google_emails": self.admin_google_emails,
            "google_client_id": self.google_client_id,
            "allow_lan_auth_bypass": self.allow_lan_auth_bypass,
            "lan_hosts": self.lan_hosts,
            "tunnel_mode": self.tunnel_mode,
            "incoming_dir": str(self.incoming_dir),
            "processed_dir": str(self.processed_dir),
            "db_path": str(self.db_path),
            "models_dir": str(self.models_dir),
            "thumbnails_dir": str(self.thumbnails_dir),
            "cameras_config_path": str(self.cameras_config_path),
            "firebase_credentials_path": str(self.firebase_credentials_path) if self.firebase_credentials_path else None,
            "webpush_topic_mode": self.webpush_topic_mode,
            "webpush_ttl_seconds": self.webpush_ttl_seconds,
        }

    def render_yaml_template(self) -> str:
        """Renders settings into a human-friendly, fully-commented YAML template."""
        # Format target classes list
        target_cls_lines = []
        for tc in self.target_classes:
            if isinstance(tc, str):
                target_cls_lines.append(f"  - {tc}")
            else:
                target_cls_lines.append(f"  - {tc}")
        target_cls_formatted = "\n".join(target_cls_lines) if target_cls_lines else "  - person\n  - car\n  - dog"

        # Format global class confidence thresholds
        class_conf_lines = []
        if self.class_confidence_thresholds:
            for cls_name, thresh in self.class_confidence_thresholds.items():
                class_conf_lines.append(f"  {cls_name}: {thresh}")
            class_conf_formatted = "\n".join(class_conf_lines)
        else:
            class_conf_formatted = "  # person: 0.50\n  # dog: 0.55"

        # Format cameras list
        cam_lines = []
        if self.cameras:
            for cam in self.cameras:
                if isinstance(cam, dict):
                    cam = CameraConfig(**cam)
                cam_lines.append(f"  - name: {json.dumps(str(cam.name))}")
                cam_lines.append(f"    serial: {json.dumps(str(cam.serial))}")
                cam_lines.append(f"    enabled: {str(cam.enabled).lower()}")
                if cam.target_classes is not None:
                    cam_lines.append(f"    target_classes: {json.dumps(cam.target_classes)}")
                if cam.confidence_threshold is not None:
                    cam_lines.append(f"    confidence_threshold: {cam.confidence_threshold}")
                if cam.class_confidence_thresholds is not None:
                    cam_lines.append("    class_confidence_thresholds:")
                    for cls_name, thresh in cam.class_confidence_thresholds.items():
                        cam_lines.append(f"      {cls_name}: {thresh}")
                if cam.sample_fps is not None:
                    cam_lines.append(f"    sample_fps: {cam.sample_fps}")
                if cam.cooldown_seconds is not None:
                    cam_lines.append(f"    cooldown_seconds: {cam.cooldown_seconds}")
                cam_lines.append("")
        cam_formatted = "\n".join(cam_lines).rstrip() if cam_lines else "  []"


        # Format Apprise URLs
        apprise_lines = []
        if self.apprise_urls:
            for url in self.apprise_urls:
                apprise_lines.append(f"  - {json.dumps(str(url))}")
        else:
            apprise_lines.append("  # - \"ntfys://ntfy.sh/my-topic?priority=high&tags=camera\"")
        apprise_formatted = "\n".join(apprise_lines)

        # Format Google Emails
        emails_lines = []
        if self.allowed_google_emails:
            for email in self.allowed_google_emails:
                emails_lines.append(f"  - {json.dumps(str(email))}")
        else:
            emails_lines.append("  # - \"user@gmail.com\"")
        emails_formatted = "\n".join(emails_lines)

        admin_lines = []
        if self.admin_google_emails:
            for email in self.admin_google_emails:
                admin_lines.append(f"  - {json.dumps(str(email))}")
        else:
            admin_lines.append("  # - \"admin@gmail.com\"")
        admin_emails_formatted = "\n".join(admin_lines)

        google_id_val = json.dumps(str(self.google_client_id)) if self.google_client_id else "null"
        fb_val = json.dumps(str(self.firebase_credentials_path)) if self.firebase_credentials_path else "null"
        domain_val = json.dumps(str(self.domain_name)) if self.domain_name else "null"
        acme_email_val = json.dumps(str(self.acme_email)) if self.acme_email else "null"
        ssl_cert_val = json.dumps(str(self.ssl_cert_path)) if self.ssl_cert_path else "null"
        ssl_key_val = json.dumps(str(self.ssl_key_path)) if self.ssl_key_path else "null"

        return f"""# ─────────────────────────────────────────────────────────────────────────────
# Sightline Surveillance Core — Settings Configuration
# Changes to this file are automatically detected and hot-reloaded at runtime.
# ─────────────────────────────────────────────────────────────────────────────

# ── Global Detection Defaults ─────────────────────────────────────────────────
# YOLO weights model file (e.g. yolo11n.pt, yolo11s.pt, yolo11m.pt)
yolo_model: {self.yolo_model}

# Default target classes for all cameras (unless overridden per camera below).
# You can use string names (e.g. ["person", "car", "dog"]) or numeric COCO IDs.
#
# Available COCO 80 classes reference:
#   Vehicles:  person (0), bicycle (1), car (2), motorcycle (3), airplane (4),
#              bus (5), train (6), truck (7), boat (8)
#   Animals:   bird (14), cat (15), dog (16), horse (17), sheep (18), cow (19),
#              elephant (20), bear (21), zebra (22), giraffe (23)
#   Outdoor:   traffic light (9), fire hydrant (10), stop sign (11), parking meter (12),
#              bench (13), backpack (24), umbrella (25), handbag (26)
target_classes:
{target_cls_formatted}

# Global minimum confidence threshold (0.01 - 1.0)
confidence_threshold: {self.confidence_threshold}

# Class-specific confidence overrides (optional)
# Any unlisted class inherits the global confidence_threshold above.
class_confidence_thresholds:
{class_conf_formatted}

# Keyframe sampling rate in FPS (frames sampled per second of video)
# (e.g. 1.0 = 1 frame/sec, 2.0 = 2 frames/sec, 0.5 = 1 frame every 2s)
sample_fps: {self.sample_fps}

# Minimum keyframes evaluated per clip to guarantee detection density on short clips
min_clip_keyframes: {self.min_clip_keyframes}

# Keyframe sample interval (legacy compatibility, kept in sync with sample_fps): 30 = 1 frame/sec for 30fps video clips
vid_stride: {self.vid_stride}

# ── Camera Definitions & Per-Camera Overrides ─────────────────────────────────
# Serial-to-name mapping and optional per-camera detection & notification overrides.
# If target_classes, confidence_threshold, or cooldown_seconds is omitted,
# the camera inherits the global defaults above.
cameras:
{cam_formatted}

# ── Notifications ────────────────────────────────────────────────────────────
# Global minimum seconds between alerts per camera (prevents alert storms)
alert_cooldown_seconds: {self.alert_cooldown_seconds}

# Apprise notification URLs (supports 80+ backends: ntfy, Pushover, Discord, etc.)
apprise_urls:
{apprise_formatted}

# Path to Firebase service account JSON for FCM push notifications
firebase_credentials_path: {fb_val}

# Web Push (PWA) RFC 8030 Topic header mode for collapsing burst notifications
# Options: "camera" (per-camera collapsing), "global" (single system-wide queue), "disabled"
webpush_topic_mode: "{self.webpush_topic_mode}"

# Web Push time-to-live in seconds (default 86400 = 24 hours)
webpush_ttl_seconds: {self.webpush_ttl_seconds}

# ── Ingestion & File Watcher ─────────────────────────────────────────────────
# Directory where camera clips are written
incoming_dir: "{self.incoming_dir}"

# Directory where clips are moved after processing
processed_dir: "{self.processed_dir}"

# Seconds of stable (unchanging) file size before processing a clip
clip_stable_seconds: {self.clip_stable_seconds}

# Scan incoming directory for unprocessed clips on startup
scan_on_startup: {str(self.scan_on_startup).lower()}

# false = inotify (recommended for local filesystem / Synology NAS)
# true  = polling fallback (use if watch_dir is an NFS/SMB network mount)
watch_use_polling: {str(self.watch_use_polling).lower()}

# ── Web UI & API Ports ──────────────────────────────────────────────────────
# Internal REST API port (default 8000 for local NAS/automations)
api_port: {self.api_port}

# Web Dashboard UI port (default 8080 - safe to expose via Reverse Proxy / Google SSO)
ui_port: {self.ui_port}

# ── HTTPS & Reverse Proxy (Caddy) ──────────────────────────────────────────
# Optional domain name for public access with automatic Let's Encrypt TLS (e.g. "cam.example.com").
# If null or empty, Caddy uses an internal CA (self-signed) for local IP / LAN access.
domain_name: {domain_val}

# Contact email for Let's Encrypt certificate renewal notices
acme_email: {acme_email_val}

# Host HTTPS port exposed by Caddy (default 8443, change to 443 if available on host)
https_port: {self.https_port}

# Host HTTP port exposed by Caddy for redirecting to HTTPS (default 8080)
http_port: {self.http_port}

# Optional custom SSL certificate and private key paths (e.g. Synology DSM certificates)
ssl_cert_path: {ssl_cert_val}
ssl_key_path: {ssl_key_val}

# Set to true when running behind Cloudflare Tunnel (uses internal TLS, Docker aliases, and CF-Connecting-IP)
tunnel_mode: {str(self.tunnel_mode).lower()}

# ── Authentication (Google SSO) ──────────────────────────────────────────────
# Allowed Google emails. If empty, authentication is bypassed (local dev mode).
allowed_google_emails:
{emails_formatted}

# Dedicated admin Google emails permitted to manage camera configurations over WAN
admin_google_emails:
{admin_emails_formatted}

# Optional Google OAuth client ID
google_client_id: {google_id_val}

# Allow requests from local LAN subnets (192.168.x.x, 10.x.x.x, etc.) to bypass Google SSO
allow_lan_auth_bypass: {str(self.allow_lan_auth_bypass).lower()}

# Optional additional LAN IP addresses or hostnames for Caddy local access
lan_hosts: {json.dumps(self.lan_hosts)}

# ── System Paths ─────────────────────────────────────────────────────────────
db_path: "{self.db_path}"
models_dir: "{self.models_dir}"
thumbnails_dir: "{self.thumbnails_dir}"
"""

    def generate_caddyfile(self) -> str:
        """Renders Caddy configuration based on active HTTPS and domain settings."""
        lines = [
            "# ─────────────────────────────────────────────────────────────────────────────",
            "# Sightline Surveillance Core — Caddy Reverse Proxy Configuration",
            "# Auto-generated from settings.yaml. Changes are hot-reloaded automatically.",
            "# ─────────────────────────────────────────────────────────────────────────────",
            "",
            "{",
            "    # Disable automatic port 80 redirects so custom ports (e.g. 8080/8443) work without conflict",
            "    auto_https disable_redirects",
            "    # Fallback SNI for direct IP access without hostname header",
            "    default_sni localhost",
            "    # Server connection limits and timeouts (mitigate Slowloris / Slow-POST attacks)",
            "    servers {",
            "        timeouts {",
            "            read_body 15s",
            "            read_header 10s",
            "            idle 2m",
            "        }",
            "    }",
        ]

        # Global email for ACME if configured
        if self.acme_email and not (self.ssl_cert_path and self.ssl_key_path):
            safe_email = re.sub(r"[\r\n\s{};\"'\\]", "", self.acme_email.strip())
            if safe_email:
                lines.append(f"    email {safe_email}")

        lines.extend([
            "}",
            "",
        ])

        backend_port = self.api_port
        target_backend = f"sightline-core:{backend_port}"

        blocked_matcher = (
            "    # Block internal management endpoints and documentation from Caddy exposure (stealth 404)\n"
            "    @blocked path /docs /docs/* /redoc /redoc/* /openapi.json /clips/process /clips/process/* "
            "/api/v1/clips/process /api/v1/clips/process/* /events /events/* /clips /clips/* /api/v1/clips /api/v1/clips/ "
            "/devices /devices/* "
            "/preferences /preferences/* "
            "/settings /settings/* /health /health/* /api/v1/health /api/v1/health/* "
            "/api/v1/events/rescan /api/v1/events/rescan/* /events/rescan /events/rescan/*\n"
            "    respond @blocked 404\n\n"
            "    # Block hidden dotfiles (.env, .git, etc.) with stealth 404\n"
            "    @hidden path /.* /.env* /.git*\n"
            "    respond @hidden 404"
        )

        https_security_headers = [
            "    # Security headers",
            '    header Strict-Transport-Security "max-age=31536000; includeSubDomains"',
            '    header X-Content-Type-Options "nosniff"',
            '    header X-Frame-Options "SAMEORIGIN"',
            '    header Referrer-Policy "strict-origin-when-cross-origin"',
            '    header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()"',
            '    header Cross-Origin-Opener-Policy "same-origin-allow-popups"',
            '    header Cross-Origin-Resource-Policy "same-origin"',
            '    header -Alt-Svc',
            '    header Content-Security-Policy "default-src \'self\'; script-src \'self\' \'unsafe-inline\' https://accounts.google.com; style-src \'self\' \'unsafe-inline\'; font-src \'self\'; img-src \'self\' data: blob: https://*.googleusercontent.com; media-src \'self\' blob:; connect-src \'self\' wss: https://accounts.google.com https://oauth2.googleapis.com https://*.googleusercontent.com; frame-src https://accounts.google.com; frame-ancestors \'self\'; object-src \'none\'; base-uri \'self\'; form-action \'self\'; upgrade-insecure-requests;"',
        ]

        # Plain HTTP block for local LAN MUST NOT include HSTS (RFC 6797 Section 7.2) or upgrade-insecure-requests
        # so browsers do not rewrite plain http:// to https:// on HTTP ports.
        http_security_headers = [
            "    # Security headers",
            '    header X-Content-Type-Options "nosniff"',
            '    header X-Frame-Options "SAMEORIGIN"',
            '    header Referrer-Policy "strict-origin-when-cross-origin"',
            '    header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()"',
            '    header Cross-Origin-Opener-Policy "same-origin-allow-popups"',
            '    header Cross-Origin-Resource-Policy "same-origin"',
            '    header -Alt-Svc',
            '    header Content-Security-Policy "default-src \'self\'; script-src \'self\' \'unsafe-inline\' https://accounts.google.com; style-src \'self\' \'unsafe-inline\'; font-src \'self\'; img-src \'self\' data: blob: https://*.googleusercontent.com; media-src \'self\' blob:; connect-src \'self\' wss: https://accounts.google.com https://oauth2.googleapis.com https://*.googleusercontent.com; frame-src https://accounts.google.com; frame-ancestors \'self\'; object-src \'none\'; base-uri \'self\'; form-action \'self\';"',
        ]

        # Helper to render the inner directives of an HTTPS site block
        def _render_site_body(is_tunnel: bool = False, explicit_tls: tuple[str, str] | None = None) -> list[str]:
            body_lines = []
            if explicit_tls:
                body_lines.append(f"    tls {explicit_tls[0]} {explicit_tls[1]}")
            elif is_tunnel:
                body_lines.append("    tls internal")
            body_lines.extend([
                "    # Remove server signature headers",
                "    header -Server",
                "    header -Via",
                "",
                *https_security_headers,
                "",
                "    # Request body size limit (prevent large buffer overflows / DoS)",
                "    request_body {",
                "        max_size 10MB",
                "    }",
                "",
                "    # Cache-busting for PWA and HTML",
                "    @nocache path / /sw.js /manifest.webmanifest",
                '    header @nocache Cache-Control "no-cache, no-store, must-revalidate, max-age=0"',
                '    header @nocache Pragma "no-cache"',
                '    header @nocache Expires "0"',
                "",
                blocked_matcher,
                "",
                "    # Serve self-hosted static assets directly from Caddy",
                "    handle_path /static/* {",
                "        header -Server",
                "        header -Via",
                "        header -Alt-Svc",
                "        root * /config/static",
                "        file_server",
                "    }",
                "",
                "    # Reverse proxy to sightline-core Web UI",
                f"    reverse_proxy {target_backend} {{",
                "        stream_timeout 24h",
                "        stream_close_delay 5m",
                "        header_up Host {http.request.host}",
                "        header_up X-Real-IP {http.request.header.Cf-Connecting-Ip}" if is_tunnel else "        header_up X-Real-IP {remote_host}",
                "        header_up X-Forwarded-Proto https",
                "        header_up X-Sightline-Access wan",
                "        header_down -Server",
                "        header_down -Via",
                "    }",
                "}",
                "",
            ])
            return body_lines

        # Main HTTPS site block
        if self.ssl_cert_path and self.ssl_key_path:
            raw_host = self.domain_name.strip() if self.domain_name else ""
            site_host = re.sub(r"[\r\n\s{};\"'\\]", "", raw_host)
            site_addr = f"{site_host}:{self.https_port}" if (site_host and self.https_port != 443) else (site_host or f":{self.https_port}")
            safe_cert = re.sub(r"[\r\n{};\"']", "", str(self.ssl_cert_path).strip())
            safe_key = re.sub(r"[\r\n{};\"']", "", str(self.ssl_key_path).strip())
            lines.append(f"{site_addr} {{")
            lines.append(f"    tls {safe_cert} {safe_key}")
            lines.extend(_render_site_body(is_tunnel=False))
        elif self.domain_name and self.domain_name.strip() and not self.domain_name.strip().replace(".", "").isdigit():
            domain = re.sub(r"[\r\n\s{};\"'\\]", "", self.domain_name.strip())
            site_addrs = [domain]
            if self.https_port != 443:
                site_addrs.append(f"{domain}:{self.https_port}")
            if self.tunnel_mode:
                site_addrs.extend([f"caddy:{self.https_port}", f"sightline-caddy:{self.https_port}"])
            lines.append(f"{', '.join(site_addrs)} {{")
            lines.extend(_render_site_body(is_tunnel=self.tunnel_mode))
        else:
            lines.append(f":{self.https_port}, localhost:{self.https_port}, 127.0.0.1:{self.https_port} {{")
            lines.extend(_render_site_body(is_tunnel=True))

        # Fallback HTTPS site block for direct IP / local LAN access
        if self.domain_name and self.domain_name.strip() and not (self.ssl_cert_path and self.ssl_key_path):
            lan_hosts = {"localhost", "127.0.0.1"}
            for h in getattr(self, "lan_hosts", []):
                cleaned = re.sub(r"[^\w.-]", "", str(h).strip())
                if cleaned:
                    lan_hosts.add(cleaned)
            # 1. Discover host/container outbound socket IP
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                lan_hosts.add(s.getsockname()[0])
                s.close()
            except Exception:
                pass
            # 2. Check if domain_name resolves to a private LAN IP (split-horizon or local DNS)
            try:
                import socket, ipaddress
                domain_clean = self.domain_name.strip().split(":")[0]
                for ip_str in socket.gethostbyname_ex(domain_clean)[2]:
                    ip_obj = ipaddress.ip_address(ip_str)
                    if ip_obj.is_private and not ip_obj.is_loopback:
                        lan_hosts.add(ip_str)
            except Exception:
                pass
            # 3. Discover default gateway and probe interface facing gateway
            try:
                import socket, ipaddress
                with open("/proc/net/route", "r") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) > 2 and parts[1] == "00000000" and parts[2] != "00000000":
                            gw_ip = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
                            gw_obj = ipaddress.ip_address(gw_ip)
                            if gw_obj.is_private and not gw_obj.is_loopback:
                                try:
                                    s_gw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                                    s_gw.connect((gw_ip, 80))
                                    lan_hosts.add(s_gw.getsockname()[0])
                                    s_gw.close()
                                except Exception:
                                    pass
            except Exception:
                pass
            # 4. Discover private IPs from /proc/net/fib_trie
            try:
                import ipaddress
                with open("/proc/net/fib_trie", "r") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) == 2 and parts[0] == "|--" and "." in parts[1]:
                            ip = parts[1]
                            if not ip.endswith(".255") and not ip.endswith(".0"):
                                try:
                                    ip_obj = ipaddress.ip_address(ip)
                                    if ip_obj.is_private and not ip_obj.is_loopback and not ip_obj.is_link_local:
                                        lan_hosts.add(ip)
                                except ValueError:
                                    pass
            except Exception:
                pass

            addrs = [f"https://{h}:{self.https_port}" for h in sorted(lan_hosts)]
            lan_addrs = ", ".join(addrs)
            lines.extend([
                f"# HTTPS for local LAN / direct IP access using internal self-signed TLS",
                f"{lan_addrs} {{",
                "    tls internal",
                "",
                "    # Remove server signature headers",
                "    header -Server",
                "    header -Via",
                "",
                "    # Reject external WAN / non-LAN clients attempting direct IP or fallback SNI access (stealth 404)",
                "    @not_lan not remote_ip 127.0.0.1 ::1 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12",
                "    respond @not_lan 404",
                "",
                *https_security_headers,
                "",
                "    # Request body size limit (prevent large buffer overflows / DoS)",
                "    request_body {",
                "        max_size 10MB",
                "    }",
                "",
                "    # Cache-busting for PWA and HTML",
                "    @nocache path / /sw.js /manifest.webmanifest",
                '    header @nocache Cache-Control "no-cache, no-store, must-revalidate, max-age=0"',
                '    header @nocache Pragma "no-cache"',
                '    header @nocache Expires "0"',
                "",
                blocked_matcher,
                "",
                "    # Serve self-hosted static assets directly from Caddy",
                "    handle_path /static/* {",
                "        header -Server",
                "        header -Via",
                "        header -Alt-Svc",
                "        root * /config/static",
                "        file_server",
                "    }",
                "",
                f"    reverse_proxy {target_backend} {{",
                "        stream_timeout 24h",
                "        stream_close_delay 5m",
                "        header_up Host {http.request.host}",
                "        header_up X-Real-IP {remote_host}",
                "        header_up X-Forwarded-Proto https",
                "        header_up X-Sightline-Access lan",
                "        header_down -Server",
                "        header_down -Via",
                "    }",
                "}",
                "",
            ])

        # HTTP block: allow plaintext Web UI access for local LAN clients, redirect public domain & WAN to HTTPS
        if self.http_port and self.http_port != self.https_port and self.http_port > 0:
            port_suffix = f":{self.https_port}" if self.https_port != 443 else ""
            redir_domain = re.sub(r"[\r\n\s{};\"'\\]", "", self.domain_name.strip()) if self.domain_name else ""
            lines.extend([
                "# HTTP: Direct Web UI proxy for local LAN, redirect public domain / WAN to HTTPS",
                f"http://:{self.http_port} {{",
                "    # Remove server signature headers",
                "    header -Server",
                "    header -Via",
                "",
                *http_security_headers,
                "",
                "    # Request body size limit (prevent large buffer overflows / DoS)",
                "    request_body {",
                "        max_size 10MB",
                "    }",
                "",
            ])
            if redir_domain:
                lines.extend([
                    "    # Redirect public domain to HTTPS",
                    f"    @public_domain host {redir_domain}",
                    f"    redir @public_domain https://{{host}}{port_suffix}{{uri}} permanent",
                    "",
                ])
            lines.extend([
                "    # Redirect non-LAN / external clients on HTTP port to HTTPS",
                "    @not_lan not remote_ip 127.0.0.1 ::1 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12",
                f"    redir @not_lan https://{redir_domain or '{host}'}{port_suffix}{{uri}} permanent",
                "",
                "    # Cache-busting for PWA and HTML",
                "    @nocache path / /sw.js /manifest.webmanifest",
                '    header @nocache Cache-Control "no-cache, no-store, must-revalidate, max-age=0"',
                '    header @nocache Pragma "no-cache"',
                '    header @nocache Expires "0"',
                "",
                blocked_matcher,
                "",
                "    # Serve self-hosted static assets directly from Caddy",
                "    handle_path /static/* {",
                "        header -Server",
                "        header -Via",
                "        header -Alt-Svc",
                "        root * /config/static",
                "        file_server",
                "    }",
                "",
                f"    reverse_proxy {target_backend} {{",
                "        stream_timeout 24h",
                "        stream_close_delay 5m",
                "        header_up Host {http.request.host}",
                "        header_up X-Real-IP {remote_host}",
                "        header_up X-Forwarded-Proto http",
                "        header_up X-Sightline-Access lan",
                "        header_down -Server",
                "        header_down -Via",
                "    }",
                "}",
                "",
            ])

        return "\n".join(lines)

    def save_caddyfile(self, path: Path | None = None) -> Path:
        """Writes current Caddy configuration to Caddyfile for the reverse proxy."""
        target = path or self.caddyfile_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self.generate_caddyfile()

            # Separate config directory from Caddy data to prevent Caddy's --watch
            # from entering an infinite reload loop when autosave.json is updated.
            caddy_data_file = target.parent.parent / "caddy_data" / "Caddyfile"
            if caddy_data_file.parent.is_dir():
                if not caddy_data_file.is_file() or caddy_data_file.read_text(encoding="utf-8") != content:
                    temp_real = caddy_data_file.with_suffix(".tmp")
                    temp_real.write_text(content, encoding="utf-8")
                    temp_real.replace(caddy_data_file)
                    logger.info(f"[config] Caddy real configuration saved to {caddy_data_file}")

                import_stmt = "import /data/Caddyfile\n"
                if not target.is_file() or target.read_text(encoding="utf-8") != import_stmt:
                    temp_target = target.with_suffix(".tmp")
                    temp_target.write_text(import_stmt, encoding="utf-8")
                    temp_target.replace(target)
                    logger.info(f"[config] Caddyfile root import saved to {target}")
                return target

            if target.is_file():
                try:
                    if target.read_text(encoding="utf-8") == content:
                        return target
                except Exception:
                    pass
            temp_target = target.with_suffix(".tmp")
            temp_target.write_text(content, encoding="utf-8")
            temp_target.replace(target)
            logger.info(f"[config] Caddyfile saved to {target}")
        except Exception as e:
            logger.warning(f"[config] failed to save Caddyfile to {target}: {e}")
        return target

    def save_to_yaml(self, path: Path | None = None) -> Path:
        """Writes current settings to the specified YAML file with comments."""
        target = path or self.settings_config_path
        if target.suffix not in (".yaml", ".yml"):
            target = target.with_suffix(".yaml")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_target = target.with_suffix(".yaml.tmp")
            content = self.render_yaml_template()
            temp_target.write_text(content, encoding="utf-8")
            temp_target.replace(target)
            logger.info(f"[config] settings saved to {target}")
        except Exception as e:
            logger.warning(f"[config] failed to save settings to {target}: {e}")
        return target

    def save_to_json(self, path: Path | None = None) -> Path:
        """Legacy JSON save fallback."""
        target = path or self.settings_config_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_target = target.with_suffix(".tmp")
            content = json.dumps(self.export_editable_dict(), indent=2) + "\n"
            temp_target.write_text(content, encoding="utf-8")
            temp_target.replace(target)
            logger.info(f"[config] settings saved to {target}")
        except Exception as e:
            logger.warning(f"[config] failed to save settings to {target}: {e}")
        return target

    def update_with(self, updates: dict[str, Any]) -> Settings:
        """Returns a new Settings instance with merged and validated updates."""
        current = self.export_editable_dict()
        for k, v in updates.items():
            if k in current:
                if v is not None or k in (
                    "domain_name",
                    "acme_email",
                    "ssl_cert_path",
                    "ssl_key_path",
                    "google_client_id",
                    "lan_hosts",
                    "yolo_model_sha256",
                ):
                    current[k] = v

        if "cameras" in updates and updates["cameras"] is not None:
            cams = []
            for c in updates["cameras"]:
                if isinstance(c, dict):
                    cams.append(CameraConfig(**c))
                else:
                    cams.append(c)
            current["cameras"] = cams

        # Synchronize sample_fps and vid_stride when updating
        if "sample_fps" in updates and updates["sample_fps"] is not None:
            try:
                fps_val = float(updates["sample_fps"])
                if fps_val >= 0.1:
                    current["vid_stride"] = max(1, round(30.0 / fps_val))
            except Exception:
                pass
        elif "vid_stride" in updates and updates["vid_stride"] is not None and "sample_fps" not in updates:
            try:
                stride_val = int(updates["vid_stride"])
                if stride_val >= 1:
                    current["sample_fps"] = round(30.0 / stride_val, 2)
            except Exception:
                pass

        current["settings_config_path"] = self.settings_config_path
        return Settings(**current)


def load_effective_settings(config_path: Path | None = None) -> Settings:
    """
    Loads base settings from environment variables, then overlays settings from
    the YAML/JSON configuration file if it exists.
    """
    base_settings = Settings()
    target_path = config_path or base_settings.settings_config_path

    if target_path.is_file():
        try:
            text = target_path.read_text(encoding="utf-8")
            data = None
            try:
                data = yaml.safe_load(text)
            except Exception:
                data = json.loads(text)

            if isinstance(data, dict):
                logger.info(f"[config] loaded settings overlay from {target_path}")
                return base_settings.update_with(data)
        except Exception as e:
            logger.warning(f"[config] failed to load {target_path}, using env/defaults: {e}")

    return base_settings


# Module-level singleton
settings = load_effective_settings()



