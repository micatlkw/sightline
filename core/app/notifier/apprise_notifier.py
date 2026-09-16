from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path

import apprise

from app.config import ALLOWED_APPRISE_SCHEMES
from app.core.helpers import format_detected_objects_summary, format_local_datetime
from app.models.event import EventRecord
from app.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


def is_allowed_apprise_scheme(url: str) -> bool:
    """Verifies that an Apprise URL uses an authorized remote notification protocol."""
    try:
        scheme = urllib.parse.urlparse(url.strip()).scheme.lower()
        return bool(scheme and scheme in ALLOWED_APPRISE_SCHEMES)
    except Exception:
        return False


class AppriseNotifier(BaseNotifier):
    """
    Delivers notifications via the Apprise library (80+ backends).

    Configure via APPRISE_URLS — a JSON list of Apprise-format URLs, e.g.:
      ["ntfys://ntfy.sh/my-topic"]
      ["pover://AppToken/UserKey"]
      ["ntfys://ntfy.sh/my-topic", "pover://token/key"]
    """

    def __init__(self, urls: list[str]) -> None:
        self._ap = apprise.Apprise()
        for url in urls:
            if not is_allowed_apprise_scheme(url):
                logger.warning(f"[notifier/apprise] disallowed URL scheme (skipped): {url[:50]}")
                continue
            if self._ap.add(url):
                # Truncate for logging; URLs may contain secrets
                display = url[:50] + ("…" if len(url) > 50 else "")
                logger.info(f"[notifier/apprise] registered: {display}")
            else:
                logger.warning(f"[notifier/apprise] invalid URL (skipped): {url[:50]}")

    def update_urls(self, urls: list[str]) -> None:
        """Dynamically reconfigures Apprise URLs at runtime."""
        new_ap = apprise.Apprise()
        for url in urls:
            if not is_allowed_apprise_scheme(url):
                logger.warning(f"[notifier/apprise] disallowed URL scheme (skipped): {url[:50]}")
                continue
            if new_ap.add(url):
                display = url[:50] + ("…" if len(url) > 50 else "")
                logger.info(f"[notifier/apprise] registered: {display}")
            else:
                logger.warning(f"[notifier/apprise] invalid URL (skipped): {url[:50]}")
        self._ap = new_ap

    async def notify(self, event: EventRecord) -> None:
        if not self._ap:
            return

        objs_summary = format_detected_objects_summary(event.objects)
        camera = event.camera_name or "Camera"
        date_str, time_str = format_local_datetime(event.detected_at)

        title = f"🚨 {camera}: {objs_summary}"
        body = (
            f"Camera: {camera}\n"
            f"Date: {date_str}\n"
            f"Time: {time_str}"
        )


        attach = [event.thumbnail] if event.thumbnail and Path(event.thumbnail).is_file() else None

        ok = False
        try:
            ok = await self._ap.async_notify(title=title, body=body, attach=attach)
        except Exception as exc:
            logger.warning(f"[notifier/apprise] notification with attachment raised exception: {exc}")

        # If attachment delivery failed (e.g. attachment size limit on ntfy.sh), fallback to text-only immediately
        if not ok and attach:
            logger.warning(f"[notifier/apprise] attachment delivery failed for event {event.id} — retrying text-only")
            try:
                ok = await self._ap.async_notify(title=title, body=body)
            except Exception as exc:
                logger.error(f"[notifier/apprise] text-only fallback raised exception: {exc}")

        if ok:
            logger.info(f"[notifier/apprise] sent for event {event.id}")
        else:
            logger.warning(f"[notifier/apprise] delivery failed for event {event.id}")


