from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.google_sso import User, get_current_user
from app.database import Database
from app.notifier.webpush import is_valid_push_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

_USER_TEST_PUSH_TS: dict[str, float] = {}
TEST_PUSH_COOLDOWN_SECONDS: float = 10.0


class SubscriptionKeys(BaseModel):
    p256dh: Optional[str] = Field(None, description="Client P-256 public key (base64url)")
    auth: Optional[str] = Field(None, description="Client authentication secret (base64url)")


class PushSubscriptionPayload(BaseModel):
    endpoint: str = Field(..., description="Push service subscription endpoint URL")
    keys: Optional[SubscriptionKeys] = Field(None, description="Client cryptographic keys")
    p256dh: Optional[str] = Field(None, description="Direct client P-256 public key (base64url)")
    auth: Optional[str] = Field(None, description="Direct client auth secret (base64url)")
    user_agent: Optional[str] = Field(None, description="Optional client browser user-agent")


class UnsubscribePayload(BaseModel):
    endpoint: str = Field(..., description="Push service subscription endpoint URL to remove")


@router.get("/vapid-public-key", summary="Get VAPID public key for Web Push subscription")
async def get_vapid_public_key(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    notifier = getattr(request.app.state, "webpush_notifier", None)
    if not notifier or not notifier.public_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Web Push notification service is initializing or unavailable",
        )
    return {
        "publicKey": notifier.public_key,
        "public_key": notifier.public_key,
    }


@router.post("/subscribe", summary="Register or update a Web Push subscription")
async def register_subscription(
    payload: PushSubscriptionPayload,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    db: Database = request.app.state.db
    p256dh = (payload.keys.p256dh if payload.keys and payload.keys.p256dh else payload.p256dh) or ""
    auth = (payload.keys.auth if payload.keys and payload.keys.auth else payload.auth) or ""

    if not p256dh or not auth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required client cryptographic keys (p256dh, auth)",
        )

    if not is_valid_push_endpoint(payload.endpoint):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or untrusted push service endpoint URL. Only authentic browser push gateways over HTTPS are permitted.",
        )

    user_agent = payload.user_agent or request.headers.get("user-agent", "")
    await db.add_web_push_subscription(
        endpoint=payload.endpoint,
        p256dh=p256dh,
        auth=auth,
        user_email=current_user.email,
        user_agent=user_agent[:255] if user_agent else None,
    )
    logger.info(f"[notifications] registered Web Push subscription for {current_user.email} ({payload.endpoint[:45]}...)")
    return {"success": True, "message": "Subscription registered successfully"}


@router.post("/unsubscribe", summary="Remove a Web Push subscription")
async def remove_subscription(
    payload: UnsubscribePayload,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    db: Database = request.app.state.db
    removed = await db.remove_web_push_subscription(payload.endpoint)
    logger.info(f"[notifications] unregistered Web Push subscription for {current_user.email} (removed={removed})")
    return {"success": True, "removed": removed}


@router.post("/test-push", summary="Send an immediate test push notification to user device(s)")
@router.post("/push-test", summary="Alias for test push")
async def send_test_push(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    notifier = getattr(request.app.state, "webpush_notifier", None)
    if not notifier:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Web Push notification service is not available",
        )

    # 10-second per-user cooldown to prevent notification flooding
    now = time.time()
    user_key = (current_user.email or "unknown").strip().lower()
    last_ts = _USER_TEST_PUSH_TS.get(user_key, 0.0)
    if now - last_ts < TEST_PUSH_COOLDOWN_SECONDS:
        remaining = int(TEST_PUSH_COOLDOWN_SECONDS - (now - last_ts)) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {remaining} second(s) before sending another test alert.",
            headers={"Retry-After": str(remaining)},
        )

    if len(_USER_TEST_PUSH_TS) > 50:
        expired_keys = [k for k, ts in _USER_TEST_PUSH_TS.items() if now - ts >= TEST_PUSH_COOLDOWN_SECONDS]
        for k in expired_keys:
            _USER_TEST_PUSH_TS.pop(k, None)

    _USER_TEST_PUSH_TS[user_key] = now

    result = await notifier.send_test_notification(user_email=current_user.email)
    result["status"] = "ok" if result.get("success") else "error"
    return result


@router.get("/status", summary="Get push notification subscription status for current user")
async def get_notification_status(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    db: Database = request.app.state.db
    all_subs = await db.get_web_push_subscriptions()
    user_subs = [s for s in all_subs if s.get("user_email", "").lower() == current_user.email.lower()]
    notifier = getattr(request.app.state, "webpush_notifier", None)
    has_keys = bool(notifier and notifier.public_key)
    return {
        "configured": has_keys,
        "total_active_subscriptions": len(all_subs),
        "total_subscriptions": len(all_subs),
        "user_subscriptions_count": len(user_subs),
    }
