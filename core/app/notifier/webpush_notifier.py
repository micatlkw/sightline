from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, List, Optional

from app.auth.google_sso import get_session_secret
from app.core.helpers import format_detected_objects_summary, format_local_datetime
from app.database import Database
from app.models.event import EventRecord
from app.notifier.base import BaseNotifier
from app.notifier.vapid import get_or_create_vapid_keys, sign_thumbnail_token
from app.notifier.webpush import sanitize_webpush_topic, send_web_push

logger = logging.getLogger(__name__)


class WebPushNotifier(BaseNotifier):
    """
    Delivers native Web Push notifications (PWA) to subscribed family devices
    using end-to-end encrypted RFC 8291 / RFC 8292 standards.
    """

    def __init__(self, db: Database, settings: Any) -> None:
        self._db = db
        self._settings = settings
        self._privkey: Optional[Any] = None
        self._pubkey_b64: Optional[str] = None
        self._vapid_sub = "mailto:" + (getattr(settings, "acme_email", None) or "admin@sightline.local")
        self._topic_mode = getattr(settings, "webpush_topic_mode", "camera") or "camera"
        self._ttl = int(getattr(settings, "webpush_ttl_seconds", 86400) or 86400)

    def update_settings(self, settings: Any) -> None:
        """Dynamically updates Web Push settings."""
        self._settings = settings
        self._topic_mode = getattr(settings, "webpush_topic_mode", self._topic_mode)
        self._ttl = int(getattr(settings, "webpush_ttl_seconds", self._ttl) or 86400)
        logger.info(f"[notifier/webpush] updated settings (topic_mode={self._topic_mode}, ttl={self._ttl}s)")

    @property
    def public_key(self) -> Optional[str]:
        return self._pubkey_b64

    async def startup(self) -> None:
        """Loads or automatically generates persistent VAPID keys on startup."""
        try:
            # Persistent location inside data dir
            storage_dir = Path(getattr(self._settings, "db_path", "/data/sightline.db")).parent
            storage_path = storage_dir / "vapid_keys.json"
            self._privkey, self._pubkey_b64 = get_or_create_vapid_keys(storage_path)
            logger.info(f"[notifier/webpush] active with VAPID public key: {self._pubkey_b64[:20]}...")
        except Exception as exc:
            logger.error(f"[notifier/webpush] startup failed to initialize VAPID keys: {exc}")

    async def notify(self, event: EventRecord) -> None:
        """Broadcasts rich notification with snapshot preview to all active subscriptions."""
        if not self._privkey or not self._pubkey_b64 or not event.objects:
            return

        subscriptions = await self._db.get_web_push_subscriptions()
        if not subscriptions:
            return

        # Clean, deduplicated, confidence-ranked capitalized object summary
        objs_summary = format_detected_objects_summary(event.objects)
        camera_name = event.camera_name or "Camera"
        date_str, time_str = format_local_datetime(event.detected_at)
        local_time_str = f"{time_str}" if time_str else (event.detected_at or "")

        title = f"🚨 {camera_name}: {objs_summary}"
        body = f"Motion detected at {local_time_str}"

        # Generate pre-signed thumbnail URL so the notification animated GIF loads without session cookies
        image_url = None
        if event.id:
            session_secret = get_session_secret(self._settings) if self._settings else "sightline-secret-key"
            sig, exp = sign_thumbnail_token(event.id, str(session_secret), ttl_seconds=900)
            image_url = f"/api/v1/events/{event.id}/thumbnail?sig={sig}&exp={exp}"

        payload = {
            "title": title,
            "body": body,
            "icon": "/static/icons/icon-192x192.png",
            "badge": "/static/icons/badge-72x72.png",
            "image": image_url,
            "data": {
                "event_id": event.id or 0,
                "url": f"/?event_id={event.id}" if event.id else "/",
            },
        }

        # Resolve RFC 8030 Topic header based on configured topic mode
        topic: Optional[str] = None
        if self._topic_mode == "camera":
            topic = sanitize_webpush_topic(event.camera_name or "camera")
        elif self._topic_mode == "global":
            topic = "sightline-alert"

        await self._dispatch_to_subscriptions(subscriptions, payload, topic=topic, ttl=self._ttl)

    async def send_test_notification(self, user_email: Optional[str] = None) -> dict[str, Any]:
        """Sends an immediate test push to verify end-to-end delivery."""
        if not self._privkey or not self._pubkey_b64:
            return {"success": False, "sent": 0, "status": "error", "message": "Web Push service not initialized"}

        subscriptions = []
        if user_email:
            subscriptions = await self._db.get_web_push_subscriptions(user_email=user_email)
            if not subscriptions:
                return {
                    "success": False,
                    "sent": 0,
                    "status": "warning",
                    "message": "No registered device found for your account. Please enable notifications on this device first.",
                }
        else:
            subscriptions = await self._db.get_web_push_subscriptions()
            if not subscriptions:
                return {
                    "success": False,
                    "sent": 0,
                    "status": "warning",
                    "message": "No registered devices found. Tap the Bell icon or 'Enable Notifications' first.",
                }

        # Attach animated GIF preview from latest event if available
        test_image_url = None
        try:
            latest_events = await self._db.get_events(limit=1)
            if latest_events and latest_events[0].id:
                latest_ev = latest_events[0]
                session_secret = get_session_secret(self._settings) if self._settings else "sightline-secret-key"
                sig, exp = sign_thumbnail_token(latest_ev.id, str(session_secret), ttl_seconds=900)
                test_image_url = f"/api/v1/events/{latest_ev.id}/thumbnail?sig={sig}&exp={exp}"
        except Exception as e:
            logger.debug(f"[notifier/webpush] could not attach preview to test alert: {e}")

        payload = {
            "title": "🔔 Sightline Test Alert",
            "body": "Native Web Push is active and working seamlessly on this device!",
            "icon": "/static/icons/icon-192x192.png",
            "badge": "/static/icons/badge-72x72.png",
            "image": test_image_url,
            "data": {
                "url": "/",
            },
        }

        # Manual test pushes use dedicated 'sightline-test' topic to collapse among tests only
        topic = "sightline-test" if self._topic_mode != "disabled" else None
        sent, pruned = await self._dispatch_to_subscriptions(subscriptions, payload, topic=topic, ttl=self._ttl)
        return {
            "success": True,
            "sent": sent,
            "pruned": pruned,
            "status": "ok",
            "message": f"Test push delivered to {sent} active device(s)!",
        }

    async def _dispatch_to_subscriptions(
        self,
        subscriptions: List[dict],
        payload: dict,
        topic: Optional[str] = None,
        ttl: int = 86400,
    ) -> tuple[int, int]:
        """Concurrently dispatches payload and automatically prunes expired endpoints (404/410)."""
        tasks = [
            send_web_push(
                sub,
                payload,
                self._privkey,
                self._pubkey_b64,
                self._vapid_sub,
                topic=topic,
                ttl=ttl,
            )
            for sub in subscriptions
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        expired_endpoints = []
        success_count = 0

        for sub, res in zip(subscriptions, results):
            endpoint = sub.get("endpoint", "")
            if isinstance(res, int):
                if res in (200, 201, 202, 204):
                    success_count += 1
                    asyncio.create_task(self._db.update_web_push_last_used(endpoint))
                elif res in (404, 410):
                    # Subscription was revoked or expired on the push service
                    expired_endpoints.append(endpoint)
                    logger.info(f"[notifier/webpush] subscription expired ({res}), marking for pruning: {endpoint[:45]}...")
                else:
                    logger.warning(f"[notifier/webpush] push service returned status {res} for {endpoint[:45]}...")
            elif isinstance(res, Exception):
                logger.warning(f"[notifier/webpush] delivery exception for {endpoint[:45]}...: {res}")

        pruned_count = 0
        if expired_endpoints:
            pruned_count = await self._db.prune_web_push_subscriptions(expired_endpoints)
            logger.info(f"[notifier/webpush] pruned {pruned_count} expired subscription(s)")

        return success_count, pruned_count
