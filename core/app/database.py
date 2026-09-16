from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from app.config import Settings
from app.core.helpers import extract_camera_name, extract_clip_datetime, resolve_camera_name
from app.models.event import ClipRecord, DetectionItem, EventRecord

logger = logging.getLogger(__name__)

# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_path    TEXT    NOT NULL,
    camera_name  TEXT,
    detected_at  TEXT    NOT NULL,
    objects      TEXT    NOT NULL,  -- JSON array of detection dicts
    thumbnail    TEXT,              -- absolute path to keyframe JPEG
    notified     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS clips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT    UNIQUE NOT NULL,
    status       TEXT    NOT NULL,  -- pending | processing | done | error
    queued_at    TEXT    NOT NULL,
    processed_at TEXT,
    error_msg    TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email   TEXT    NOT NULL,
    device_id    TEXT    UNIQUE NOT NULL,
    fcm_token    TEXT    NOT NULL,
    device_name  TEXT,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_email   TEXT    NOT NULL,
    camera_name  TEXT    NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    mute_until   TEXT,
    PRIMARY KEY (user_email, camera_name)
);

CREATE TABLE IF NOT EXISTS revoked_tokens (
    token_hash   TEXT    PRIMARY KEY,
    revoked_at   TEXT    NOT NULL,
    expires_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_session_revocations (
    user_email      TEXT    PRIMARY KEY,
    revoked_before  INTEGER NOT NULL,
    revoked_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS web_push_subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint     TEXT    UNIQUE NOT NULL,
    p256dh       TEXT    NOT NULL,
    auth         TEXT    NOT NULL,
    user_email   TEXT    NOT NULL,
    user_agent   TEXT,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_camera       ON events(camera_name);
CREATE INDEX IF NOT EXISTS idx_events_detected     ON events(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_clips_status        ON clips(status);
CREATE INDEX IF NOT EXISTS idx_devices_user        ON devices(user_email);
CREATE INDEX IF NOT EXISTS idx_revoked_tokens_exp  ON revoked_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_push_sub_user       ON web_push_subscriptions(user_email);
CREATE INDEX IF NOT EXISTS idx_push_sub_endpoint   ON web_push_subscriptions(endpoint);
"""


class Database:
    """
    Async SQLite database wrapper.  Manages schema initialization, clip state
    tracking, and event log persistence.
    """

    def __init__(self, settings: Settings) -> None:
        self._db_path = settings.db_path
        self._settings = settings
        self._incoming_dir = settings.incoming_dir
        self._processed_dir = settings.processed_dir
        self._watch_dir = settings.incoming_dir
        self._conn: Optional[aiosqlite.Connection] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not create database directory {self._db_path.parent}: {e}")
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_DDL)
        await self._conn.commit()

        # Migrate legacy camera serials/filenames to camera names
        if self._settings and hasattr(self._settings, "cameras"):
            for cam in self._settings.cameras:
                if cam.name and cam.serial:
                    await self._conn.execute(
                        "UPDATE events SET camera_name = ? WHERE camera_name = ? OR camera_name LIKE ?",
                        (cam.name, cam.serial, f"{cam.serial}_%"),
                    )
            await self._conn.commit()

        # Recover any unindexed processed video clips with detection suffixes
        await self.recover_unindexed_events()

        # Prune expired session token revocations
        try:
            await self.prune_expired_revocations()
        except Exception as e:
            logger.warning(f"Could not prune expired token revocations: {e}")

        logger.info(f"Database connected: {self._db_path}")

    async def recover_unindexed_events(self, days: int = 3, full: bool = False) -> int:
        """
        Scans processed_dir for clips with detection suffixes that are not yet
        indexed in SQLite events table, and registers them automatically.
        By default, limits scan to recent `days` (default: 3 days) for blazing-fast startup.
        Pass full=True to scan all historical folders.
        """
        if not self._processed_dir or not Path(self._processed_dir).is_dir():
            return 0

        proc_dir = Path(self._processed_dir)
        recovered_count = 0
        try:
            # 1. Determine date filter window
            date_filter_prefixes: set[str] = set()
            cutoff_date_str = ""
            if not full and days > 0:
                now = datetime.now()
                recent_dates = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]
                date_filter_prefixes = set(recent_dates)
                cutoff_date_str = min(recent_dates)

            # 2. Pre-fetch known indexed filenames in a single query (O(1) memory lookup)
            known_filenames: set[str] = set()
            if cutoff_date_str:
                query = "SELECT clip_path FROM events WHERE detected_at >= ?"
                params = (cutoff_date_str,)
            else:
                query = "SELECT clip_path FROM events"
                params = ()

            async with self._conn.execute(query, params) as cur:
                rows = await cur.fetchall()
                for row in rows:
                    p = row[0] if isinstance(row, (tuple, list)) else row["clip_path"]
                    if p:
                        known_filenames.add(Path(p).name)

            # 3. Collect candidate MP4 files to inspect
            candidate_files: list[Path] = []
            if date_filter_prefixes:
                # Fast targeted scan only in recent date directories
                for item in proc_dir.iterdir():
                    if item.is_dir():
                        # Case A: processed_dir / YYYY-MM-DD / <camera>
                        if item.name in date_filter_prefixes:
                            candidate_files.extend(item.rglob("*.mp4"))
                        # Case B: processed_dir / <camera> / YYYY-MM-DD
                        else:
                            for sub in item.iterdir():
                                if sub.is_dir() and sub.name in date_filter_prefixes:
                                    candidate_files.extend(sub.rglob("*.mp4"))
            else:
                # Full scan
                candidate_files.extend(proc_dir.rglob("*.mp4"))

            # 4. Check candidates against known filenames set
            for mp4_path in candidate_files:
                stem = mp4_path.stem
                if "-" not in stem:
                    continue

                if mp4_path.name in known_filenames:
                    continue

                # Parse detection suffix (support -none for zero-detection clips)
                objects = []
                if not stem.endswith("-none"):
                    suffix_part = stem.split("-", 1)[1]
                    raw_tokens = suffix_part.split("-")
                    for token in raw_tokens:
                        m = re.match(r"^(\d+)([a-zA-Z_]+)$", token)
                        if m:
                            count, cls_name = int(m.group(1)), m.group(2)
                            for _ in range(count):
                                objects.append({
                                    "class": cls_name,
                                    "class_id": 0,
                                    "confidence": 0.85,
                                    "timestamp_sec": 0.0,
                                    "bbox": [0.0, 0.0, 1.0, 1.0],
                                    "keyframe_path": None,
                                })
                        elif token and token != "none":
                            objects.append({
                                "class": token,
                                "class_id": 0,
                                "confidence": 0.85,
                                "timestamp_sec": 0.0,
                                "bbox": [0.0, 0.0, 1.0, 1.0],
                                "keyframe_path": None,
                            })

                if not objects and not stem.endswith("-none"):
                    continue

                camera_name = extract_camera_name(
                    mp4_path,
                    self._incoming_dir,
                    [self._processed_dir, self._incoming_dir],
                    custom_mapping=self._settings.get_camera_mapping() if self._settings else None,
                )
                detected_at_iso = extract_clip_datetime(mp4_path).isoformat()

                thumbnail_path = None
                if self._settings and hasattr(self._settings, "thumbnails_dir") and self._settings.thumbnails_dir:
                    t_dir = Path(self._settings.thumbnails_dir)
                    date_str = detected_at_iso[:10]
                    candidates = [
                        t_dir / date_str / camera_name / f"{stem}.jpg",
                        t_dir / date_str / camera_name / f"{stem}.gif",
                        t_dir / date_str / f"{stem}.jpg",
                        t_dir / date_str / f"{stem}.gif",
                        t_dir / camera_name / date_str / f"{stem}.jpg",
                        t_dir / camera_name / date_str / f"{stem}.gif",
                    ]
                    for c in candidates:
                        if c.is_file():
                            thumbnail_path = str(c)
                            break
                    if not thumbnail_path:
                        base_stem = stem.split("-")[0]
                        matches = list(t_dir.glob(f"**/{base_stem}*.[gj][ip][fg]*"))
                        if matches:
                            thumbnail_path = str(matches[0])

                objects_json = json.dumps(objects)
                await self._conn.execute(
                    """
                    INSERT INTO events (clip_path, camera_name, detected_at, objects, thumbnail, notified)
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (str(mp4_path), camera_name, detected_at_iso, objects_json, thumbnail_path),
                )
                known_filenames.add(mp4_path.name)
                recovered_count += 1
                logger.info(f"[database] Recovered unindexed event for {mp4_path.name} (camera: {camera_name})")

            if recovered_count > 0:
                await self._conn.commit()
                logger.info(f"[database] Successfully recovered {recovered_count} unindexed event(s) into database")
        except Exception as exc:
            logger.warning(f"[database] Auto-recovery scan encountered error: {exc}")

        return recovered_count

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── Clip tracking ─────────────────────────────────────────────────────────

    async def mark_clip(
        self,
        path: Path,
        status: str,
        error_msg: Optional[str] = None,
        target_path: Optional[Path] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        processed_at = now if status in ("done", "error") else None
        final_path = str(target_path or path)

        if target_path and target_path != path:
            async with self._conn.execute(
                "SELECT id FROM clips WHERE path = ?", (str(path),)
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                await self._conn.execute(
                    """
                    UPDATE clips
                    SET path = ?, status = ?, processed_at = ?, error_msg = ?
                    WHERE path = ?
                    """,
                    (final_path, status, processed_at, error_msg, str(path)),
                )
                await self._conn.commit()
                return

        await self._conn.execute(
            """
            INSERT INTO clips (path, status, queued_at, processed_at, error_msg)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                status       = excluded.status,
                processed_at = COALESCE(excluded.processed_at, processed_at),
                error_msg    = excluded.error_msg
            """,
            (final_path, status, now, processed_at, error_msg),
        )
        await self._conn.commit()

    async def get_clips(self, limit: int = 50, offset: int = 0) -> list[ClipRecord]:
        async with self._conn.execute(
            "SELECT * FROM clips ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
        return [ClipRecord(**dict(row)) for row in rows]

    async def get_done_clip_paths(self) -> set[str]:
        """Returns a set of all clip paths marked with status 'done'."""
        async with self._conn.execute(
            "SELECT path FROM clips WHERE status = 'done'"
        ) as cur:
            rows = await cur.fetchall()
        return {row["path"] for row in rows}

    # ── Event persistence ─────────────────────────────────────────────────────

    async def save_event(
        self,
        clip_path: Path,
        detections: list[DetectionItem],
        camera_name: Optional[str] = None,
        detected_at: Optional[str] = None,
    ) -> EventRecord:
        if not camera_name:
            camera_name = extract_camera_name(
                clip_path,
                self._incoming_dir,
                [self._processed_dir, self._incoming_dir, self._watch_dir],
                custom_mapping=self._settings.get_camera_mapping(),
            )

        if detected_at:
            event_time_iso = detected_at
        else:
            base_dt = extract_clip_datetime(clip_path)
            # Offset by the first detection keyframe timestamp within the clip
            first_offset = next((d.timestamp_sec for d in detections if d.timestamp_sec), 0.0)
            event_dt = base_dt + timedelta(seconds=first_offset) if first_offset > 0 else base_dt
            event_time_iso = event_dt.isoformat()

        # Use the first keyframe with a saved thumbnail as the event thumbnail
        thumbnail = next(
            (d.keyframe_path for d in detections if d.keyframe_path), None
        )
        if not thumbnail and self._settings and hasattr(self._settings, "thumbnails_dir") and self._settings.thumbnails_dir:
            t_dir = Path(self._settings.thumbnails_dir)
            date_str = event_time_iso[:10]
            candidates = [
                t_dir / date_str / camera_name / f"{clip_path.stem}.gif",
                t_dir / date_str / f"{clip_path.stem}.gif",
                t_dir / camera_name / date_str / f"{clip_path.stem}.gif",
            ]
            for c in candidates:
                if c.is_file():
                    thumbnail = str(c)
                    break

        objects = [
            {
                "class_id": d.class_id,
                "class": d.class_name,
                "confidence": round(d.confidence, 4),
                "timestamp_sec": round(d.timestamp_sec, 2),
                "bbox": d.bbox,
                "keyframe_path": d.keyframe_path,
            }
            for d in detections
        ]
        objects_json = json.dumps(objects)

        async with self._conn.execute(
            """
            INSERT INTO events (clip_path, camera_name, detected_at, objects, thumbnail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(clip_path), camera_name, event_time_iso, objects_json, thumbnail),
        ) as cur:
            event_id = cur.lastrowid

        await self._conn.commit()
        logger.debug(f"Saved event {event_id} with {len(detections)} detections")

        return EventRecord(
            id=event_id,
            clip_path=str(clip_path),
            camera_name=camera_name,
            detected_at=event_time_iso,
            objects=objects,
            thumbnail=thumbnail,
            notified=0,
        )

    async def get_events(
        self,
        camera: Optional[str] = None,
        cls: Optional[str] = None,
        date: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[EventRecord]:
        query = "SELECT * FROM events WHERE 1=1"
        params: list = []

        if camera:
            serial = None
            if self._settings and hasattr(self._settings, "get_camera_config"):
                cam_cfg = self._settings.get_camera_config(camera)
                if cam_cfg:
                    serial = cam_cfg.serial
            if serial:
                query += " AND (camera_name = ? OR camera_name = ? OR camera_name LIKE ?)"
                params.extend([camera, serial, f"{serial}_%"])
            else:
                query += " AND (camera_name = ? OR camera_name LIKE ?)"
                params.extend([camera, f"{camera}_%"])
        if cls:
            if cls.lower() in ("none", "no_detection", "no_detections", "empty"):
                query += " AND (objects = '[]' OR objects IS NULL OR objects = '')"
            elif cls.lower() == "all_including_none":
                pass  # Do not filter by detections
            else:
                # JSON text search — good enough for moderate event volumes
                query += ' AND objects LIKE ?'
                params.append(f'%"class": "{cls}"%')
        else:
            # Default: show events with qualifying detections
            query += " AND (objects != '[]' AND objects IS NOT NULL AND objects != '')"
        if date:
            query += " AND detected_at LIKE ?"
            params.append(f"{date}%")
        if start_date:
            query += " AND detected_at >= ?"
            params.append(start_date)
        if end_date:
            query += " AND detected_at <= ?"
            params.append(end_date)

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        mapping = self._settings.get_camera_mapping() if self._settings and hasattr(self._settings, "get_camera_mapping") else None
        async with self._conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(row, mapping) for row in rows]

    async def get_event(self, event_id: int) -> Optional[EventRecord]:
        mapping = self._settings.get_camera_mapping() if self._settings and hasattr(self._settings, "get_camera_mapping") else None
        async with self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_event(row, mapping) if row else None

    async def get_distinct_dates(self) -> list[str]:
        """Returns list of distinct YYYY-MM-DD dates that have detection events, descending."""
        async with self._conn.execute(
            "SELECT DISTINCT substr(detected_at, 1, 10) as dt FROM events WHERE detected_at IS NOT NULL ORDER BY dt DESC LIMIT 60"
        ) as cur:
            rows = await cur.fetchall()
            return [row["dt"] for row in rows if row["dt"]]

    async def get_distinct_cameras(self) -> list[str]:
        """Returns list of distinct camera names that have detection events."""
        async with self._conn.execute(
            "SELECT DISTINCT camera_name FROM events WHERE camera_name IS NOT NULL ORDER BY camera_name ASC"
        ) as cur:
            rows = await cur.fetchall()
            return [row["camera_name"] for row in rows if row["camera_name"]]

    async def mark_notified(self, event_id: int) -> None:
        await self._conn.execute(
            "UPDATE events SET notified = 1 WHERE id = ?", (event_id,)
        )
        await self._conn.commit()

    async def update_event_thumbnail(self, event_id: int, thumbnail_path: str) -> None:
        await self._conn.execute(
            "UPDATE events SET thumbnail = ? WHERE id = ?", (thumbnail_path, event_id)
        )
        await self._conn.commit()

    async def update_event_clip_path(self, event_id: int, clip_path: str) -> None:
        await self._conn.execute(
            "UPDATE events SET clip_path = ? WHERE id = ?", (clip_path, event_id)
        )
        await self._conn.commit()

    async def update_event_objects(self, event_id: int, detections: list[DetectionItem]) -> None:
        objects = [
            {
                "class_id": d.class_id,
                "class": d.class_name,
                "confidence": round(d.confidence, 4),
                "timestamp_sec": round(d.timestamp_sec, 2),
                "bbox": d.bbox,
                "keyframe_path": d.keyframe_path,
            }
            for d in detections
        ]
        objects_json = json.dumps(objects)
        await self._conn.execute(
            "UPDATE events SET objects = ? WHERE id = ?", (objects_json, event_id)
        )
        await self._conn.commit()

    async def delete_event(self, event_id: int) -> Optional[dict[str, Any]]:
        """
        Deletes an event from the database, along with any associated clip records.
        Returns metadata dict of the deleted event, or None if not found.
        """
        async with self._conn.execute(
            "SELECT id, clip_path, thumbnail, camera_name, detected_at FROM events WHERE id = ?",
            (event_id,),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        event_data = dict(row)
        clip_path = event_data.get("clip_path")

        await self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        if clip_path:
            clip_name = Path(clip_path).name
            await self._conn.execute(
                "DELETE FROM clips WHERE path = ? OR path LIKE ?",
                (clip_path, f"%{clip_name}"),
            )
        await self._conn.commit()
        return event_data

    async def delete_events_batch(self, event_ids: list[int]) -> list[dict[str, Any]]:
        """
        Batch deletes multiple events from the database in a single transaction.
        Returns list of metadata dicts for the deleted events.
        """
        if not event_ids:
            return []

        deleted_records = []
        for eid in event_ids:
            async with self._conn.execute(
                "SELECT id, clip_path, thumbnail, camera_name, detected_at FROM events WHERE id = ?",
                (eid,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                deleted_records.append(dict(row))

        if not deleted_records:
            return []

        for record in deleted_records:
            eid = record["id"]
            clip_path = record.get("clip_path")
            await self._conn.execute("DELETE FROM events WHERE id = ?", (eid,))
            if clip_path:
                clip_name = Path(clip_path).name
                await self._conn.execute(
                    "DELETE FROM clips WHERE path = ? OR path LIKE ?",
                    (clip_path, f"%{clip_name}"),
                )

        await self._conn.commit()
        return deleted_records

    async def prune_missing_events(
        self,
        days: int = 7,
        full: bool = False,
        settings: Optional[Settings] = None,
    ) -> list[int]:
        """
        Scans events in the SQLite database and checks whether their physical .mp4 video
        file exists on disk. If the MP4 file is missing, the orphaned event record
        and its clip associations are removed from the database, and any lingering
        thumbnail variants are deleted.
        Returns the list of pruned event IDs.
        """
        if not self._conn:
            return []

        # Local import to prevent circular dependency
        from app.api.routes.events import delete_event_physical_files, find_event_clip_path

        cfg = settings or self._settings
        mapping = (
            cfg.get_camera_mapping()
            if cfg and hasattr(cfg, "get_camera_mapping")
            else None
        )

        cutoff_date_str = ""
        if not full and days > 0:
            now = datetime.now()
            cutoff_date_str = (now - timedelta(days=days)).strftime("%Y-%m-%d")

        if cutoff_date_str:
            query = "SELECT * FROM events WHERE detected_at >= ?"
            params: tuple[Any, ...] = (cutoff_date_str,)
        else:
            query = "SELECT * FROM events"
            params = ()

        async with self._conn.execute(query, params) as cur:
            rows = await cur.fetchall()

        if not rows:
            return []

        orphaned_ids: list[int] = []
        for row in rows:
            event = _row_to_event(row, mapping)
            if not event:
                continue
            clip_path = find_event_clip_path(event, cfg)
            if not clip_path or not clip_path.is_file():
                orphaned_ids.append(event.id)
                try:
                    delete_event_physical_files(event, cfg)
                except Exception as exc:
                    logger.warning(
                        f"[database] Failed to delete physical remnants for orphaned event #{event.id}: {exc}"
                    )
                await self._conn.execute("DELETE FROM events WHERE id = ?", (event.id,))
                if event.clip_path:
                    c_name = Path(event.clip_path).name
                    await self._conn.execute(
                        "DELETE FROM clips WHERE path = ? OR path LIKE ?",
                        (event.clip_path, f"%{c_name}"),
                    )

        if orphaned_ids:
            await self._conn.commit()
            logger.info(
                f"[database] Pruned {len(orphaned_ids)} orphaned event(s) from database: {orphaned_ids}"
            )

        return orphaned_ids

    # ── Devices & Push Notifications ──────────────────────────────────────────

    async def register_device(
        self,
        user_email: str,
        device_id: str,
        fcm_token: str,
        device_name: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO devices (user_email, device_id, fcm_token, device_name, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                user_email  = excluded.user_email,
                fcm_token   = excluded.fcm_token,
                device_name = excluded.device_name,
                updated_at  = excluded.updated_at
            """,
            (user_email.strip().lower(), device_id, fcm_token, device_name, now),
        )
        await self._conn.commit()

    async def get_user_devices(self, user_email: str) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM devices WHERE user_email = ? ORDER BY id DESC",
            (user_email.strip().lower(),),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_device(self, user_email: str, device_id: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM devices WHERE user_email = ? AND device_id = ?",
            (user_email.strip().lower(), device_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def remove_fcm_token(self, fcm_token: str) -> None:
        await self._conn.execute(
            "DELETE FROM devices WHERE fcm_token = ?",
            (fcm_token,),
        )
        await self._conn.commit()

    # ── Notification Preferences ──────────────────────────────────────────────

    async def set_user_camera_preference(
        self,
        user_email: str,
        camera_name: str,
        enabled: bool = True,
        mute_until: Optional[str] = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO user_preferences (user_email, camera_name, enabled, mute_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_email, camera_name) DO UPDATE SET
                enabled    = excluded.enabled,
                mute_until = excluded.mute_until
            """,
            (user_email.strip().lower(), camera_name, 1 if enabled else 0, mute_until),
        )
        await self._conn.commit()

    async def get_user_camera_preferences(self, user_email: str) -> dict[str, dict]:
        async with self._conn.execute(
            "SELECT camera_name, enabled, mute_until FROM user_preferences WHERE user_email = ?",
            (user_email.strip().lower(),),
        ) as cur:
            rows = await cur.fetchall()
        return {
            row["camera_name"]: {
                "enabled": bool(row["enabled"]),
                "mute_until": row["mute_until"],
            }
            for row in rows
        }

    async def get_subscribed_fcm_tokens(self, camera_name: str) -> list[str]:
        """
        Retrieves all FCM tokens for users who have not disabled or muted alerts for camera_name.
        """
        now = datetime.now(timezone.utc).isoformat()
        query = """
        SELECT d.fcm_token
        FROM devices d
        LEFT JOIN user_preferences p 
            ON d.user_email = p.user_email AND p.camera_name = ?
        WHERE (p.enabled IS NULL OR p.enabled = 1)
          AND (p.mute_until IS NULL OR p.mute_until < ?)
        """
        async with self._conn.execute(query, (camera_name, now)) as cur:
            rows = await cur.fetchall()
        return [row["fcm_token"] for row in rows]

    # ── Token Revocation ───────────────────────────────────────────────────────

    async def revoke_token(self, token_hash: str, expires_at: int) -> None:
        """Records a revoked token hash to prevent its reuse."""
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO revoked_tokens (token_hash, revoked_at, expires_at)
            VALUES (?, ?, ?)
            """,
            (token_hash, now_iso, expires_at),
        )
        await self._conn.commit()

    async def is_token_revoked(self, token_hash: str) -> bool:
        """Returns True if the token hash has been recorded as revoked."""
        async with self._conn.execute(
            "SELECT 1 FROM revoked_tokens WHERE token_hash = ?",
            (token_hash,),
        ) as cur:
            row = await cur.fetchone()
            return row is not None

    async def prune_expired_revocations(self) -> int:
        """Removes expired revoked token records to prevent database growth."""
        now_ts = int(datetime.now(timezone.utc).timestamp())
        async with self._conn.execute(
            "DELETE FROM revoked_tokens WHERE expires_at < ?",
            (now_ts,),
        ) as cur:
            pruned = cur.rowcount
        await self._conn.commit()
        return pruned

    async def get_user_session_revocations(self) -> dict[str, int]:
        """Loads all active user session revocations into a dict {user_email: revoked_before_ts}."""
        async with self._conn.execute("SELECT user_email, revoked_before FROM user_session_revocations") as cur:
            rows = await cur.fetchall()
            return {row["user_email"].lower(): int(row["revoked_before"]) for row in rows}

    async def revoke_user_sessions(self, user_email: str, revoked_before: int) -> None:
        """Records a user-level session revocation timestamp to invalidate prior sessions."""
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO user_session_revocations (user_email, revoked_before, revoked_at)
            VALUES (?, ?, ?)
            """,
            (user_email.strip().lower(), revoked_before, now_iso),
        )
        await self._conn.commit()

    async def prune_expired_user_revocations(self, max_age_seconds: int = 7 * 86400) -> int:
        """Removes user revocation timestamps older than max session lifespan (7 days)."""
        cutoff_ts = int(time.time()) - max_age_seconds
        async with self._conn.execute(
            "DELETE FROM user_session_revocations WHERE revoked_before < ?",
            (cutoff_ts,),
        ) as cur:
            pruned = cur.rowcount
        await self._conn.commit()
        return pruned

    # ── Web Push Subscriptions ────────────────────────────────────────────────

    async def add_web_push_subscription(
        self,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_email: str,
        user_agent: str | None = None,
    ) -> None:
        """Saves or updates a Web Push subscription for a user device."""
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO web_push_subscriptions (endpoint, p256dh, auth, user_email, user_agent, created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh,
                auth = excluded.auth,
                user_email = excluded.user_email,
                user_agent = COALESCE(excluded.user_agent, web_push_subscriptions.user_agent),
                last_used_at = excluded.last_used_at
            """,
            (endpoint.strip(), p256dh.strip(), auth.strip(), user_email.strip().lower(), user_agent, now_iso, now_iso),
        )
        await self._conn.commit()

    async def remove_web_push_subscription(self, endpoint: str) -> bool:
        """Removes a Web Push subscription by its unique endpoint."""
        async with self._conn.execute(
            "DELETE FROM web_push_subscriptions WHERE endpoint = ?",
            (endpoint.strip(),),
        ) as cur:
            removed = cur.rowcount > 0
        await self._conn.commit()
        return removed

    async def get_web_push_subscriptions(self, user_email: str | None = None) -> list[dict]:
        """Retrieves active Web Push subscriptions, optionally filtered by user_email."""
        if user_email:
            query = "SELECT * FROM web_push_subscriptions WHERE user_email = ? ORDER BY last_used_at DESC"
            params = (user_email.strip().lower(),)
        else:
            query = "SELECT * FROM web_push_subscriptions ORDER BY last_used_at DESC"
            params = ()

        async with self._conn.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def update_web_push_last_used(self, endpoint: str) -> None:
        """Updates the last_used_at timestamp for a subscription."""
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE web_push_subscriptions SET last_used_at = ? WHERE endpoint = ?",
            (now_iso, endpoint.strip()),
        )
        await self._conn.commit()

    async def prune_web_push_subscriptions(self, endpoints: list[str]) -> int:
        """Prunes dead or unsubscribed endpoints (e.g. 404/410 from push services)."""
        if not endpoints:
            return 0
        placeholders = ",".join("?" for _ in endpoints)
        async with self._conn.execute(
            f"DELETE FROM web_push_subscriptions WHERE endpoint IN ({placeholders})",
            endpoints,
        ) as cur:
            pruned = cur.rowcount
        await self._conn.commit()
        return pruned



# ── Helpers ───────────────────────────────────────────────────────────────────


def _row_to_event(row: aiosqlite.Row, mapping: dict[str, str] | None = None) -> EventRecord:
    d = dict(row)
    d["objects"] = json.loads(d["objects"])
    if d.get("camera_name"):
        d["camera_name"] = resolve_camera_name(d["camera_name"], custom_mapping=mapping)
    return EventRecord(**d)
