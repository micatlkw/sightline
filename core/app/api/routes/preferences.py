from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.auth.google_sso import User, get_current_user
from app.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/preferences", tags=["preferences"])

_PREF_WRITE_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_PREF_RATE_LIMIT_WINDOW = 60.0  # seconds
_MAX_PREF_WRITES_PER_WINDOW = 30
_MAX_PREF_TRACKED_USERS = 2000


def check_preference_rate_limit(user_email: str, endpoint: str = "/api/v1/preferences") -> None:
    now = time.time()
    user_key = user_email.lower().strip()

    if len(_PREF_WRITE_ATTEMPTS) > 50:
        expired = [
            u for u, timestamps in _PREF_WRITE_ATTEMPTS.items()
            if not timestamps or now - timestamps[-1] >= _PREF_RATE_LIMIT_WINDOW
        ]
        for u in expired:
            _PREF_WRITE_ATTEMPTS.pop(u, None)

    if len(_PREF_WRITE_ATTEMPTS) >= _MAX_PREF_TRACKED_USERS and user_key not in _PREF_WRITE_ATTEMPTS:
        oldest = min(_PREF_WRITE_ATTEMPTS.keys(), key=lambda k: _PREF_WRITE_ATTEMPTS[k][-1] if _PREF_WRITE_ATTEMPTS[k] else 0)
        _PREF_WRITE_ATTEMPTS.pop(oldest, None)

    attempts = _PREF_WRITE_ATTEMPTS[user_key]
    valid_attempts = [t for t in attempts if now - t < _PREF_RATE_LIMIT_WINDOW]
    if len(valid_attempts) >= _MAX_PREF_WRITES_PER_WINDOW:
        logger.warning(f"[AUDIT] [RATE_LIMITED] user {user_email} exceeded preference write rate limit on {endpoint}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many preference modification requests. Please try again in a minute.",
            headers={"Retry-After": "60"},
        )
    valid_attempts.append(now)
    _PREF_WRITE_ATTEMPTS[user_key] = valid_attempts


class CameraPreferenceRequest(BaseModel):
    camera_name: str = Field(..., min_length=1, max_length=64)
    enabled: bool = True
    mute_until: Optional[str] = Field(None, max_length=64)  # ISO-8601 UTC timestamp e.g. "2026-08-24T18:00:00Z"

    @field_validator("camera_name")
    @classmethod
    def _validate_camera_name(cls, v: str) -> str:
        s = str(v).strip()
        if not s:
            raise ValueError("camera_name cannot be empty")
        if re.search(r"[\r\n\x00-\x1f]", s):
            raise ValueError("camera_name must not contain control characters or newlines")
        return s

    @field_validator("mute_until")
    @classmethod
    def _validate_mute_until(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        try:
            iso_str = s.replace("Z", "+00:00") if s.endswith("Z") else s
            dt = datetime.fromisoformat(iso_str)
        except Exception:
            raise ValueError("mute_until must be a valid ISO-8601 timestamp (e.g. 2026-08-24T18:00:00Z)")

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        now = datetime.now(timezone.utc)
        if dt <= now:
            raise ValueError("mute_until timestamp must be in the future")

        max_allowed = now + timedelta(days=30)
        if dt > max_allowed:
            raise ValueError("mute_until cannot exceed 30 days into the future")

        return dt.isoformat()


@router.get("", summary="Get camera notification preferences for current user")
async def get_preferences(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    db: Database = request.app.state.db
    prefs = await db.get_user_camera_preferences(user_email=current_user.email)
    return {
        "user_email": current_user.email,
        "preferences": prefs,
    }


@router.put("", summary="Set camera notification preference for current user")
async def update_preference(
    body: CameraPreferenceRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    check_preference_rate_limit(current_user.email, "/api/v1/preferences")
    db: Database = request.app.state.db
    await db.set_user_camera_preference(
        user_email=current_user.email,
        camera_name=body.camera_name,
        enabled=body.enabled,
        mute_until=body.mute_until,
    )
    logger.info(
        f"[AUDIT] [PREFERENCE_UPDATED] user={current_user.email} camera={body.camera_name} enabled={body.enabled} mute_until={body.mute_until or 'none'}"
    )
    return {
        "status": "updated",
        "user_email": current_user.email,
        "camera_name": body.camera_name,
        "enabled": body.enabled,
        "mute_until": body.mute_until,
    }
