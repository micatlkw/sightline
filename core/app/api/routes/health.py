from __future__ import annotations

from datetime import datetime, timezone
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth.google_sso import User, get_current_user, is_admin_user, is_lan_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness and pipeline status")
async def health(request: Request) -> dict:
    pipeline = request.app.state.pipeline
    watcher = request.app.state.watcher
    settings = request.app.state.settings

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "watcher": {
            "alive": watcher.is_alive,
            "mode": watcher.mode,
            "incoming_configured": bool(settings.incoming_dir),
            "processed_configured": bool(settings.processed_dir),
        },
        "pipeline": {
            "processed": pipeline.processed,
            "errors": pipeline.errors,
        },
        "detection": {
            "model": settings.yolo_model,
            "target_classes": settings.target_classes,
            "confidence_threshold": settings.confidence_threshold,
            "vid_stride": settings.vid_stride,
        },
    }


@router.post("/notifications/test", summary="Send a test notification to all configured notifiers")
@router.post("/health/notification-test", summary="Send a test notification (UI alias)")
async def test_notification(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    cfg = getattr(request.app.state, "settings", None)
    is_lan = is_lan_client(request, cfg) if cfg else False

    # Check admin privileges over WAN
    if not is_lan and not is_admin_user(current_user, cfg):
        logger.warning(f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} denied test notification over WAN")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to send test notifications over WAN",
        )

    logger.info(f"[AUDIT] [ADMIN_ACTION] User {current_user.email} initiated test notification")

    # Enforce 30-second cooldown to prevent notification spam / alert storms
    now_ts = time.time()
    last_ts = getattr(request.app.state, "_last_test_notification_ts", 0.0)
    cooldown = 30.0
    if now_ts - last_ts < cooldown:
        remaining = int(cooldown - (now_ts - last_ts)) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {remaining} second(s) before sending another test notification.",
        )
    request.app.state._last_test_notification_ts = now_ts

    from app.models.event import EventRecord

    notifier = getattr(request.app.state, "notifier", None)
    if not notifier:
        return {"status": "error", "detail": "Notifier not initialized"}

    now_iso = datetime.now(timezone.utc).isoformat()
    test_event = EventRecord(
        id=0,
        clip_path="/data/test.mp4",
        camera_name="TestCamera",
        detected_at=now_iso,
        objects=[{"class": "person", "confidence": 0.99}],
        thumbnail=None,
    )

    results = []
    notifiers_list = getattr(notifier, "_notifiers", [notifier])
    for n in notifiers_list:
        name = type(n).__name__
        try:
            await n.notify(test_event)
            results.append({"notifier": name, "status": "sent"})
        except Exception as exc:
            results.append({"notifier": name, "status": "error", "error": str(exc)})

    return {"status": "ok", "dispatched_at": now_iso, "results": results}

