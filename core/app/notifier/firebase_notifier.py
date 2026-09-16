from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

import cv2
import firebase_admin
from firebase_admin import credentials, messaging

from app.core.helpers import format_local_datetime
from app.database import Database
from app.models.event import EventRecord
from app.notifier.base import BaseNotifier


logger = logging.getLogger(__name__)


class FirebaseNotifier(BaseNotifier):
    """
    Sends high-priority push notifications to registered Android devices
    via Google Firebase Cloud Messaging (FCM).
    """

    def __init__(
        self,
        credentials_path: Optional[Path],
        db: Database,
    ) -> None:
        self._credentials_path = credentials_path
        self._db = db
        self._app: Optional[firebase_admin.App] = None
        self._initialized = False

    async def startup(self) -> None:
        if not self._credentials_path or not self._credentials_path.is_file():
            logger.info(
                f"[fcm] credentials not found at {self._credentials_path} — FCM disabled"
            )
            return

        try:
            cred = credentials.Certificate(str(self._credentials_path))
            try:
                self._app = firebase_admin.get_app("sightline")
            except ValueError:
                self._app = firebase_admin.initialize_app(cred, name="sightline")
            self._initialized = True
            logger.info(f"[fcm] Firebase initialized successfully from {self._credentials_path}")
        except Exception as exc:
            logger.warning(f"[fcm] failed to initialize Firebase Admin SDK: {exc}")

    async def shutdown(self) -> None:
        if self._app:
            try:
                firebase_admin.delete_app(self._app)
            except Exception:
                pass
            self._app = None
            self._initialized = False

    async def notify(self, event: EventRecord) -> None:
        if not self._initialized:
            return

        camera_name = event.camera_name or "camera"
        tokens = await self._db.get_subscribed_fcm_tokens(camera_name)
        if not tokens:
            logger.debug(f"[fcm] no registered/active devices for camera '{camera_name}'")
            return

        # Prepare summary of detected objects
        classes = sorted({obj["class"] for obj in event.objects if "class" in obj})
        top_conf = max((obj.get("confidence", 0.0) for obj in event.objects), default=0.0)

        labels_summary = ", ".join(classes)

        date_str, time_str = format_local_datetime(event.detected_at)
        title = f"🚨 {camera_name.title()}: {labels_summary.title()} Detected"
        body = f"{labels_summary.title()} ({top_conf:.0%} conf) on {date_str} at {time_str}"


        # Generate lightweight base64 thumbnail preview if thumbnail exists
        preview_b64 = ""
        if event.thumbnail and Path(event.thumbnail).is_file():
            try:
                import io
                from PIL import Image

                with Image.open(event.thumbnail) as pil_img:
                    pil_img.seek(0)
                    frame = pil_img.convert("RGB")
                    w, h = frame.size
                    scale = min(240 / w, 240 / h)
                    if scale < 1.0:
                        new_w = max(1, int(w * scale))
                        new_h = max(1, int(h * scale))
                        frame = frame.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    frame.save(buf, format="JPEG", quality=60)
                    preview_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception as exc:
                logger.warning(f"[fcm] error generating thumbnail preview: {exc}")


        data_payload = {
            "event_id": str(event.id),
            "camera_name": camera_name,
            "detected_at": event.detected_at,
            "labels": labels_summary,
            "thumbnail_url": f"/api/v1/events/{event.id}/thumbnail",
            "preview_b64": preview_b64,
        }

        # Build FCM Multicast Message
        message = messaging.MulticastMessage(
            tokens=tokens,
            data=data_payload,
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="sightline_alerts",
                    sound="default",
                    click_action="OPEN_EVENT_ACTIVITY",
                ),
            ),
        )

        try:
            response = messaging.send_each_for_multicast(message, app=self._app)
            logger.info(
                f"[fcm] alert dispatched: {response.success_count}/{len(tokens)} devices succeeded"
            )

            # Cleanup expired/unregistered tokens
            for idx, resp in enumerate(response.responses):
                if not resp.success:
                    err = resp.exception
                    if isinstance(err, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
                        bad_token = tokens[idx]
                        logger.info(f"[fcm] removing stale device token: {bad_token[:12]}…")
                        await self._db.remove_fcm_token(bad_token)
        except Exception as exc:
            logger.error(f"[fcm] error sending multicast push: {exc}", exc_info=True)
