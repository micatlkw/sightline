from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.auth.google_sso import User, get_current_user, is_admin_user, is_lan_client
from app.core.pipeline import Pipeline
from app.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clips", tags=["clips"])

_LAST_SCAN_TS: float = 0.0
SCAN_COOLDOWN_SECONDS: float = 300.0  # 5 minutes


class ProcessClipRequest(BaseModel):
    path: str


@router.get("", summary="List clip processing history")
async def list_clips(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
) -> dict:
    db: Database = request.app.state.db
    clips = await db.get_clips(limit=limit, offset=offset)
    return {
        "clips": [dataclasses.asdict(c) for c in clips],
        "limit": limit,
        "offset": offset,
    }


@router.post("/process", status_code=202, summary="Manually enqueue a clip")
async def process_clip(
    request: Request,
    body: ProcessClipRequest,
    current_user: User = Depends(get_current_user),
) -> dict:
    clip_path = Path(body.path)
    incoming_dir = Path(request.app.state.settings.incoming_dir).resolve()
    try:
        resolved_path = clip_path.resolve()
        if not resolved_path.is_relative_to(incoming_dir):
            raise HTTPException(
                status_code=400,
                detail="Clip path must be located within incoming directory",
            )
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid clip path")

    if not resolved_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {body.path}")
    if resolved_path.suffix.lower() != ".mp4":
        raise HTTPException(status_code=400, detail="Only .mp4 files are supported")

    pipeline: Pipeline = request.app.state.pipeline
    await pipeline.enqueue(resolved_path)

    return {"status": "queued", "path": str(resolved_path)}


@router.post("/scan", status_code=200, summary="Scan incoming directory for unprocessed clips")
async def scan_clips(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> dict:
    settings = getattr(request.app.state, "settings", None)
    is_lan = is_lan_client(request, settings)

    # Over WAN, only administrators can initiate directory rescans
    if not is_lan and not is_admin_user(current_user, settings):
        logger.warning(f"[AUDIT] [AUTH_DENIED] Non-admin user {current_user.email} denied clips scan over WAN")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to trigger clips rescan over WAN",
        )

    # Rate limiting cooldown (protect Synology NAS from I/O thrashing)
    global _LAST_SCAN_TS
    now = time.time()
    elapsed = now - _LAST_SCAN_TS
    if elapsed < SCAN_COOLDOWN_SECONDS:
        retry_after = int(SCAN_COOLDOWN_SECONDS - elapsed)
        response.headers["Retry-After"] = str(retry_after)
        logger.warning(f"[AUDIT] [RATE_LIMITED] User {current_user.email} triggered clips scan rate limit")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rescan cooldown active. Please wait {retry_after} second(s) before requesting another scan.",
        )

    _LAST_SCAN_TS = now
    pipeline: Pipeline = request.app.state.pipeline
    enqueued = await pipeline.scan_and_enqueue()
    logger.info(f"[AUDIT] [ADMIN_ACTION] User {current_user.email} scanned incoming clips, enqueued: {len(enqueued)}")
    return {
        "status": "ok",
        "queued_count": len(enqueued),
    }
