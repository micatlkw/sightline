from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
import re
import time

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.utils.range_stream import range_stream_response
from app.auth.google_sso import User, get_current_user, get_session_secret, is_admin_user, is_lan_client
from app.config import Settings, settings
from app.core.helpers import extract_date_str, resolve_camera_name
from app.database import Database
from app.notifier.vapid import verify_thumbnail_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

_LAST_RESCAN_TS: float = 0.0
RESCAN_COOLDOWN_SECONDS: float = 300.0  # 5 minutes


def sanitize_disposition_filename(filename: str) -> str:
    """Strips control characters, newlines, quotes, and path separators from attachment filenames."""
    cleaned = re.sub(r'[\r\n"\\/;`]', '_', filename).strip('_ ')
    return cleaned or "download"


def is_safe_media_path(p: Path, settings) -> bool:
    """Verifies that resolved path is strictly contained within designated media directories."""
    if not settings:
        return True
    allowed_roots: list[Path] = []
    for attr in ("thumbnails_dir", "processed_dir", "incoming_dir"):
        val = getattr(settings, attr, None)
        if val:
            try:
                allowed_roots.append(Path(val).resolve())
            except Exception:
                pass
    if not allowed_roots:
        return True
    try:
        resolved = p.resolve()
        return any(resolved.is_relative_to(root) for root in allowed_roots)
    except Exception:
        return False


def find_event_thumbnail_path(event, settings) -> Path | None:
    """
    Robustly resolves the thumbnail file path for an event across legacy serial folders,
    camera name folders, date hierarchies, and container mount paths.
    """
    # 1. Direct path check
    if event.thumbnail:
        p = Path(event.thumbnail)
        if p.is_file() and is_safe_media_path(p, settings):
            return p

    # 2. Extract key parts for fallback search
    thumb_filename = Path(event.thumbnail).name if event.thumbnail else ""
    clip_filename = Path(event.clip_path).name if event.clip_path else ""

    candidate_names: list[str] = []
    if thumb_filename:
        candidate_names.append(thumb_filename)
    if clip_filename:
        stem = Path(clip_filename).stem
        candidate_names.append(f"{stem}.gif")
        candidate_names.append(f"{stem}.jpg")
        candidate_names.append(f"{stem}.jpeg")
        if "-" in stem:
            base_stem = stem.split("-")[0]
            candidate_names.append(f"{base_stem}.gif")
            candidate_names.append(f"{base_stem}.jpg")
    elif thumb_filename and "-" in thumb_filename:
        base = Path(thumb_filename).stem.split('-')[0]
        candidate_names.append(f"{base}.gif")
        candidate_names.append(f"{base}.jpg")

    date_str = ""
    if event.detected_at:
        date_str = event.detected_at[:10]
    elif clip_filename:
        try:
            date_str = extract_date_str(Path(clip_filename))
        except Exception:
            pass

    cam_name = event.camera_name or ""
    cam_serial = ""
    if settings and hasattr(settings, "get_camera_config") and cam_name:
        cam_cfg = settings.get_camera_config(cam_name)
        if cam_cfg:
            cam_serial = cam_cfg.serial or ""

    search_roots: list[Path] = []
    if settings and hasattr(settings, "thumbnails_dir") and settings.thumbnails_dir:
        search_roots.append(Path(settings.thumbnails_dir))
    if event.clip_path:
        search_roots.append(Path(event.clip_path).parent)

    # 3. Direct / Structured Folder Lookups
    for root in search_roots:
        if not root.is_dir():
            continue
        for cname in candidate_names:
            if (root / cname).is_file():
                return root / cname
            if date_str:
                if cam_name and (root / date_str / cam_name / cname).is_file():
                    return root / date_str / cam_name / cname
                if cam_serial and (root / date_str / cam_serial / cname).is_file():
                    return root / date_str / cam_serial / cname
                if (root / date_str / cname).is_file():
                    return root / date_str / cname
                if cam_name and (root / cam_name / date_str / cname).is_file():
                    return root / cam_name / date_str / cname
                if cam_serial and (root / cam_serial / date_str / cname).is_file():
                    return root / cam_serial / date_str / cname

    # 4. Glob Fallback under thumbnails root
    if settings and hasattr(settings, "thumbnails_dir") and settings.thumbnails_dir:
        root = Path(settings.thumbnails_dir)
        if root.is_dir():
            for name in candidate_names:
                stem = Path(name).stem
                if date_str and (root / date_str).is_dir():
                    matches = list((root / date_str).glob(f"**/{stem}*.gif"))
                    if matches:
                        return matches[0]
                    matches_j = list((root / date_str).glob(f"**/{stem}*.jp*g"))
                    if matches_j:
                        return matches_j[0]
                matches = list(root.glob(f"**/{stem}*.gif"))
                if matches:
                    return matches[0]
                matches_jpg = list(root.glob(f"**/{stem}*.jp*g"))
                if matches_jpg:
                    return matches_jpg[0]

    # 5. On-demand first frame extraction from clip MP4 if thumbnail doesn't exist
    clip_file = find_event_clip_path(event, settings)
    if clip_file and clip_file.is_file() and settings and hasattr(settings, "thumbnails_dir"):
        try:
            import cv2
            t_dir = Path(settings.thumbnails_dir)
            target_dir = t_dir / (date_str or "misc") / (cam_name or "camera")
            target_dir.mkdir(parents=True, exist_ok=True)
            target_jpg = target_dir / f"{clip_file.stem}.jpg"
            cap = cv2.VideoCapture(str(clip_file))
            if cap.isOpened():
                ret, frame = cap.read()
                cap.release()
                if ret and frame is not None:
                    h, w = frame.shape[:2]
                    target_w = 480
                    if w > target_w:
                        frame = cv2.resize(frame, (target_w, int(h * (target_w / w))), interpolation=cv2.INTER_AREA)
                    cv2.imwrite(str(target_jpg), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    return target_jpg
        except Exception:
            pass

    return None


def find_event_clip_path(event, settings) -> Path | None:
    """
    Robustly resolves the processed video MP4 path for an event.
    """
    if event.clip_path:
        p = Path(event.clip_path)
        if p.is_file() and is_safe_media_path(p, settings):
            return p

    clip_filename = Path(event.clip_path).name if event.clip_path else ""
    if not clip_filename:
        return None

    date_str = event.detected_at[:10] if event.detected_at else ""
    cam_name = event.camera_name or ""
    cam_serial = ""
    if settings and hasattr(settings, "get_camera_config") and cam_name:
        cam_cfg = settings.get_camera_config(cam_name)
        if cam_cfg:
            cam_serial = cam_cfg.serial or ""

    search_roots: list[Path] = []
    if settings and hasattr(settings, "processed_dir") and settings.processed_dir:
        search_roots.append(Path(settings.processed_dir))

    for root in search_roots:
        if not root.is_dir():
            continue
        if (root / clip_filename).is_file():
            return root / clip_filename
        if date_str:
            if cam_name and (root / cam_name / date_str / clip_filename).is_file():
                return root / cam_name / date_str / clip_filename
            if cam_serial and (root / cam_serial / date_str / clip_filename).is_file():
                return root / cam_serial / date_str / clip_filename
            if (root / date_str / cam_name / clip_filename).is_file():
                return root / date_str / cam_name / clip_filename
            if (root / date_str / cam_serial / clip_filename).is_file():
                return root / date_str / cam_serial / clip_filename

    # Glob fallback
    if search_roots and search_roots[0].is_dir():
        root = search_roots[0]
        stem = Path(clip_filename).stem.split("-")[0]
        matches = list(root.glob(f"**/{stem}*.mp4"))
        if matches:
            return matches[0]

    return None


@router.get("", summary="List detection events")
async def list_events(
    request: Request,
    camera: str | None = Query(None, max_length=64, description="Filter by camera name"),
    cls: str | None = Query(None, max_length=64, description="Filter by class name, e.g. 'person'"),
    date: str | None = Query(None, max_length=32, pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$", description="Filter by date YYYY-MM-DD"),
    start_date: str | None = Query(None, max_length=32, description="Start date ISO/YYYY-MM-DD"),
    end_date: str | None = Query(None, max_length=32, description="End date ISO/YYYY-MM-DD"),
    limit: int = Query(10, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
) -> dict:
    db: Database = request.app.state.db
    events = await db.get_events(
        camera=camera,
        cls=cls,
        date=date,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    return {
        "events": [e.to_dict() for e in events],
        "limit": limit,
        "offset": offset,
    }


@router.post("/rescan", summary="Rescan processed directory, recover unindexed events, and prune deleted events")
async def rescan_events(
    request: Request,
    days: int = Query(7, ge=1, le=365, description="Number of recent days to scan"),
    full: bool = Query(False, description="Scan all historical dates"),
    current_user: User = Depends(get_current_user),
) -> dict:
    global _LAST_RESCAN_TS
    cfg = getattr(request.app.state, "settings", None)
    is_lan = is_lan_client(request, cfg) if cfg else False

    # 1. Require administrator privileges over WAN
    if not is_lan and not is_admin_user(current_user, cfg):
        logger.warning(f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} denied event rescan over WAN")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to trigger event rescan over WAN",
        )

    # 2. Enforce 5-minute cooldown to protect NAS storage drives from I/O exhaustion
    now = time.time()
    if now - _LAST_RESCAN_TS < RESCAN_COOLDOWN_SECONDS:
        retry_after = int(RESCAN_COOLDOWN_SECONDS - (now - _LAST_RESCAN_TS))
        logger.warning(f"[AUDIT] [RATE_LIMITED] User {current_user.email} triggered event rescan rate limit ({retry_after}s remaining)")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rescan cooldown active. Please wait {retry_after}s before requesting another scan.",
            headers={"Retry-After": str(retry_after)},
        )

    _LAST_RESCAN_TS = now
    logger.info(f"[AUDIT] [ADMIN_ACTION] User {current_user.email} initiated event rescan & sync (days={days}, full={full})")
    db: Database = request.app.state.db
    recovered = await db.recover_unindexed_events(days=days, full=full)
    pruned_ids = await db.prune_missing_events(days=days, full=full, settings=cfg)

    bus = getattr(request.app.state, "bus", None)
    if bus and pruned_ids:
        for eid in pruned_ids:
            bus.publish("event.deleted", {"id": eid})

    return {
        "status": "ok",
        "recovered": recovered,
        "pruned": len(pruned_ids),
        "days_scanned": "all" if full else days,
    }


@router.get("/filters", summary="Get available filter options (dates, cameras, classes)")
async def get_event_filters(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    db: Database = request.app.state.db
    dates = await db.get_distinct_dates()
    settings = getattr(request.app.state, "settings", None)
    mapping = settings.get_camera_mapping() if settings and hasattr(settings, "get_camera_mapping") else {}

    # Configured cameras from settings
    configured_cameras = [c.name for c in settings.cameras if c.enabled is not False] if settings and hasattr(settings, "cameras") else []
    
    # DB recorded cameras resolved to human-readable names
    db_cameras = await db.get_distinct_cameras()
    resolved_db = [resolve_camera_name(c, custom_mapping=mapping) for c in db_cameras]

    # Deduplicate and prioritize clean camera names
    clean_cameras = set(configured_cameras)
    for cam in resolved_db:
        if cam and not ("_" in cam and any(char.isdigit() for char in cam)):
            clean_cameras.add(cam)

    all_cameras = sorted(list(clean_cameras))
    return {
        "dates": dates,
        "cameras": all_cameras,
    }


@router.get("/{event_id}", summary="Get a single detection event")
async def get_event(
    event_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    db: Database = request.app.state.db
    event = await db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event.to_dict()


@router.get("/{event_id}/thumbnail", summary="Serve or download the keyframe thumbnail (GIF/JPEG) for an event")
async def get_thumbnail(
    event_id: int,
    request: Request,
    download: bool = Query(False, description="Set attachment header to trigger direct file download"),
    sig: str | None = Query(None, description="HMAC signature for signed Web Push thumbnail access"),
    exp: int | None = Query(None, description="Expiration timestamp for signed Web Push thumbnail access"),
) -> FileResponse:
    settings = getattr(request.app.state, "settings", None)
    session_secret = get_session_secret(settings) if settings else "sightline-secret-key"

    # 1. Allow access if valid signed token is provided (for background lock-screen push notifications)
    if sig and exp:
        if not verify_thumbnail_token(event_id, sig, exp, session_secret):
            raise HTTPException(status_code=403, detail="Invalid or expired thumbnail signature")
    else:
        # 2. Otherwise enforce standard user session authentication
        await get_current_user(request)

    db: Database = request.app.state.db
    event = await db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    thumb_path = find_event_thumbnail_path(event, settings)
    if not thumb_path or not thumb_path.is_file() or not is_safe_media_path(thumb_path, settings):
        raise HTTPException(status_code=404, detail="No thumbnail found for this event")

    # Update database record if resolved to a new path
    if event.thumbnail != str(thumb_path):
        try:
            await db.update_event_thumbnail(event.id, str(thumb_path))
        except Exception:
            pass

    media_type, _ = mimetypes.guess_type(str(thumb_path))
    safe_filename = sanitize_disposition_filename(thumb_path.name)
    headers: dict[str, str] = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{safe_filename}"'

    if sig and exp:
        # Cache-Control capped to remaining HMAC validity (up to 900s / 15m)
        now_ts = int(time.time())
        remaining = max(0, min(900, exp - now_ts))
        headers["Cache-Control"] = f"private, max-age={remaining}"
    else:
        headers["Cache-Control"] = "private, max-age=1800"

    return FileResponse(str(thumb_path), media_type=media_type or "image/gif", headers=headers)


@router.get("/{event_id}/video", summary="Stream or download the MP4 video clip for an event")
async def get_video(
    event_id: int,
    request: Request,
    download: bool = Query(False, description="Set attachment header to trigger direct file download"),
    current_user: User = Depends(get_current_user),
) -> Response:
    db: Database = request.app.state.db
    settings = getattr(request.app.state, "settings", None)
    event = await db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    clip_path = find_event_clip_path(event, settings)
    if not clip_path or not clip_path.is_file() or not is_safe_media_path(clip_path, settings):
        raise HTTPException(status_code=404, detail=f"Clip file not found: {event.clip_path}")

    # Update database record if resolved to a new path
    if event.clip_path != str(clip_path):
        try:
            await db.update_event_clip_path(event.id, str(clip_path))
        except Exception:
            pass

    if download:
        safe_filename = sanitize_disposition_filename(clip_path.name)
        return FileResponse(
            str(clip_path),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_filename}"',
                "Cache-Control": "private, max-age=1800",
            },
        )

    return range_stream_response(clip_path, request)


class BatchDeleteRequest(BaseModel):
    event_ids: list[int]


def delete_event_physical_files(event, settings: Settings | None) -> list[str]:
    """
    Locates and unlinks all physical media files associated with an event:
    the processed MP4 video, JPEG keyframe/thumbnail, and GIF highlights.
    Returns list of unlinked file paths.
    """
    unlinked: list[str] = []

    # 1. MP4 video clip
    clip_p = find_event_clip_path(event, settings)
    if clip_p and clip_p.is_file() and is_safe_media_path(clip_p, settings):
        try:
            clip_p.unlink(missing_ok=True)
            unlinked.append(str(clip_p))
        except Exception as e:
            logger.warning(f"[events] Failed to unlink video clip {clip_p}: {e}")

    if event.clip_path:
        p = Path(event.clip_path)
        if p.is_file() and is_safe_media_path(p, settings) and str(p) not in unlinked:
            try:
                p.unlink(missing_ok=True)
                unlinked.append(str(p))
            except Exception as e:
                logger.warning(f"[events] Failed to unlink event.clip_path {p}: {e}")

    # 2. Keyframe thumbnail
    thumb_p = find_event_thumbnail_path(event, settings)
    if thumb_p and thumb_p.is_file() and is_safe_media_path(thumb_p, settings):
        try:
            thumb_p.unlink(missing_ok=True)
            unlinked.append(str(thumb_p))
        except Exception as e:
            logger.warning(f"[events] Failed to unlink thumbnail {thumb_p}: {e}")

    if event.thumbnail:
        p = Path(event.thumbnail)
        if p.is_file() and is_safe_media_path(p, settings) and str(p) not in unlinked:
            try:
                p.unlink(missing_ok=True)
                unlinked.append(str(p))
            except Exception as e:
                logger.warning(f"[events] Failed to unlink event.thumbnail {p}: {e}")

    # 3. Clean up related highlight GIFs and JPEG variants in thumbnails_dir
    if settings and hasattr(settings, "thumbnails_dir") and settings.thumbnails_dir:
        t_dir = Path(settings.thumbnails_dir)
        if t_dir.is_dir():
            stems_to_check = set()
            if event.clip_path:
                c_stem = Path(event.clip_path).stem
                stems_to_check.add(c_stem)
                if "-" in c_stem:
                    stems_to_check.add(c_stem.split("-")[0])
            if event.thumbnail:
                t_stem = Path(event.thumbnail).stem
                stems_to_check.add(t_stem)
                if "-" in t_stem:
                    stems_to_check.add(t_stem.split("-")[0])

            for stem in stems_to_check:
                if not stem or len(stem) < 3:
                    continue
                # Match stem*.gif and stem*.jp*g anywhere under thumbnails_dir
                for matched in t_dir.glob(f"**/{stem}*"):
                    if matched.is_file() and is_safe_media_path(matched, settings) and str(matched) not in unlinked:
                        try:
                            matched.unlink(missing_ok=True)
                            unlinked.append(str(matched))
                        except Exception as e:
                            logger.warning(f"[events] Failed to unlink thumbnail variant {matched}: {e}")

    return unlinked


@router.delete("/{event_id}", summary="Delete an event record and associated video/thumbnail files (Admin only)")
@router.post("/{event_id}/delete", summary="Delete an event record (POST alias for environments blocking DELETE)")
async def delete_event(
    event_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cfg: Settings = getattr(request.app.state, "settings", settings)
    if not is_admin_user(current_user, cfg):
        logger.warning(f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} attempted to delete event #{event_id}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to delete events",
        )

    db: Database = request.app.state.db
    event = await db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Event #{event_id} not found")

    unlinked_files = delete_event_physical_files(event, cfg)
    deleted = await db.delete_event(event_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Event #{event_id} not found")

    bus = getattr(request.app.state, "bus", None)
    if bus:
        bus.publish("event.deleted", {"id": event_id})

    logger.info(f"[AUDIT] [EVENT_DELETED] admin={current_user.email} event_id={event_id} files_removed={len(unlinked_files)}")
    return {
        "status": "deleted",
        "id": event_id,
        "unlinked_files": unlinked_files,
    }


@router.post("/batch-delete", summary="Batch delete multiple events and their video/thumbnail files (Admin only)")
async def batch_delete_events(
    body: BatchDeleteRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cfg: Settings = getattr(request.app.state, "settings", settings)
    if not is_admin_user(current_user, cfg):
        logger.warning(f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} attempted to batch delete events: {body.event_ids}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to delete events",
        )

    db: Database = request.app.state.db
    bus = getattr(request.app.state, "bus", None)
    deleted_ids: list[int] = []
    failed_ids: list[int] = []
    total_unlinked: list[str] = []

    for eid in body.event_ids:
        try:
            event = await db.get_event(eid)
            if not event:
                failed_ids.append(eid)
                continue
            unlinked = delete_event_physical_files(event, cfg)
            total_unlinked.extend(unlinked)
            res = await db.delete_event(eid)
            if res:
                deleted_ids.append(eid)
                if bus:
                    bus.publish("event.deleted", {"id": eid})
            else:
                failed_ids.append(eid)
        except Exception as exc:
            logger.error(f"[events] Error deleting event #{eid}: {exc}")
            failed_ids.append(eid)

    logger.info(f"[AUDIT] [EVENTS_BATCH_DELETED] admin={current_user.email} deleted={deleted_ids} failed={failed_ids} files_removed={len(total_unlinked)}")
    return {
        "status": "deleted",
        "deleted_ids": deleted_ids,
        "failed_ids": failed_ids,
        "unlinked_files_count": len(total_unlinked),
    }

