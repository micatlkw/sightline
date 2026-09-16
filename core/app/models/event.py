from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DetectionItem:
    """A single object detection within a keyframe."""

    class_id: int
    class_name: str
    confidence: float
    timestamp_sec: float
    bbox: list[float]           # [x1, y1, x2, y2] normalised 0..1
    keyframe_path: str | None = None


@dataclass
class EventRecord:
    """A detection event — one clip may produce one EventRecord with N detections."""

    id: int | None
    clip_path: str
    camera_name: str | None
    detected_at: str            # ISO 8601
    objects: list[dict[str, Any]]
    thumbnail: str | None
    notified: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the canonical JSON API shape."""
        filename = Path(self.clip_path).name if self.clip_path else ""
        return {
            "id": self.id,
            "clip_path": filename,
            "clip_filename": filename,
            "video_url": (
                f"/api/v1/events/{self.id}/video" if self.id else None
            ),
            "camera_name": self.camera_name,
            "detected_at": self.detected_at,
            "thumbnail_url": (
                f"/api/v1/events/{self.id}/thumbnail" if self.thumbnail else None
            ),
            "objects": self.objects,
        }


@dataclass
class ClipRecord:
    """Processing state for a single .mp4 clip."""

    id: int
    path: str
    status: str             # pending | processing | done | error
    queued_at: str
    processed_at: str | None = None
    error_msg: str | None = None
