from __future__ import annotations

from collections import defaultdict
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.auth.google_sso import User, get_current_user
from app.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"])

_DEVICE_WRITE_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_DEVICE_RATE_LIMIT_WINDOW = 60.0  # seconds
_MAX_DEVICE_WRITES_PER_WINDOW = 30
_MAX_DEVICE_TRACKED_USERS = 2000


def check_device_rate_limit(user_email: str, endpoint: str = "/api/v1/devices") -> None:
    now = time.time()
    user_key = user_email.lower().strip()

    if len(_DEVICE_WRITE_ATTEMPTS) > 50:
        expired = [
            u for u, timestamps in _DEVICE_WRITE_ATTEMPTS.items()
            if not timestamps or now - timestamps[-1] >= _DEVICE_RATE_LIMIT_WINDOW
        ]
        for u in expired:
            _DEVICE_WRITE_ATTEMPTS.pop(u, None)

    if len(_DEVICE_WRITE_ATTEMPTS) >= _MAX_DEVICE_TRACKED_USERS and user_key not in _DEVICE_WRITE_ATTEMPTS:
        oldest = min(_DEVICE_WRITE_ATTEMPTS.keys(), key=lambda k: _DEVICE_WRITE_ATTEMPTS[k][-1] if _DEVICE_WRITE_ATTEMPTS[k] else 0)
        _DEVICE_WRITE_ATTEMPTS.pop(oldest, None)

    attempts = _DEVICE_WRITE_ATTEMPTS[user_key]
    valid_attempts = [t for t in attempts if now - t < _DEVICE_RATE_LIMIT_WINDOW]
    if len(valid_attempts) >= _MAX_DEVICE_WRITES_PER_WINDOW:
        logger.warning(f"[AUDIT] [RATE_LIMITED] user {user_email} exceeded device write rate limit on {endpoint}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many device modification requests. Please try again in a minute.",
            headers={"Retry-After": "60"},
        )
    valid_attempts.append(now)
    _DEVICE_WRITE_ATTEMPTS[user_key] = valid_attempts


class DeviceRegisterRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)
    fcm_token: str = Field(..., min_length=1, max_length=4096)
    device_name: Optional[str] = Field(None, max_length=128)

    @field_validator("device_id")
    @classmethod
    def _validate_device_id(cls, v: str) -> str:
        if re.search(r"[\r\n\x00-\x1f]", str(v)):
            raise ValueError("device_id must not contain control characters or newlines")
        s = str(v).strip()
        if not s:
            raise ValueError("device_id cannot be empty")
        return s

    @field_validator("fcm_token")
    @classmethod
    def _validate_fcm_token(cls, v: str) -> str:
        if re.search(r"[\r\n\x00-\x1f]", str(v)):
            raise ValueError("fcm_token must not contain control characters or newlines")
        s = str(v).strip()
        if not s:
            raise ValueError("fcm_token cannot be empty")
        return s

    @field_validator("device_name")
    @classmethod
    def _validate_device_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if re.search(r"[\r\n\x00-\x1f]", str(v)):
            raise ValueError("device_name must not contain control characters or newlines")
        s = str(v).strip()
        if not s:
            return None
        return s


@router.post("/register", summary="Register or update mobile device FCM token")
async def register_device(
    body: DeviceRegisterRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    check_device_rate_limit(current_user.email, "/api/v1/devices/register")
    db: Database = request.app.state.db
    await db.register_device(
        user_email=current_user.email,
        device_id=body.device_id,
        fcm_token=body.fcm_token,
        device_name=body.device_name,
    )
    logger.info(f"[AUDIT] [DEVICE_REGISTERED] user={current_user.email} device_id={body.device_id} device_name={body.device_name or 'unnamed'}")
    return {
        "status": "registered",
        "user_email": current_user.email,
        "device_id": body.device_id,
    }


@router.get("", summary="List registered devices for the current user")
async def list_devices(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    db: Database = request.app.state.db
    devices = await db.get_user_devices(user_email=current_user.email)
    return {"devices": devices}


@router.delete("/{device_id}", summary="Unregister a device")
async def unregister_device(
    device_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict:
    check_device_rate_limit(current_user.email, f"/api/v1/devices/{device_id}")
    db: Database = request.app.state.db
    deleted = await db.delete_device(user_email=current_user.email, device_id=device_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    logger.info(f"[AUDIT] [DEVICE_UNREGISTERED] user={current_user.email} device_id={device_id}")
    return {"status": "unregistered", "device_id": device_id}
