from __future__ import annotations

import logging
import time
from typing import Any

from app.models.event import EventRecord
from app.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


class CompositeNotifier(BaseNotifier):
    """
    Chains one or more *BaseNotifier* instances and enforces a per-camera
    cooldown to prevent alert storms when a camera generates burst detections.

    If no notifiers are configured, *notify* is a no-op.
    """

    def __init__(
        self,
        notifiers: list[BaseNotifier],
        cooldown_seconds: int = 60,
    ) -> None:
        self._notifiers = notifiers
        self._cooldown = cooldown_seconds
        # camera_name → monotonic timestamp of last successful notification
        self._last_notified: dict[str, float] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        for n in self._notifiers:
            await n.startup()

    async def shutdown(self) -> None:
        for n in self._notifiers:
            await n.shutdown()

    def update_settings(self, settings: Any) -> None:
        """Dynamically updates notification cooldown and notifier URLs."""
        self._cooldown = getattr(settings, "alert_cooldown_seconds", self._cooldown)
        for notifier in self._notifiers:
            if hasattr(notifier, "update_urls") and hasattr(settings, "apprise_urls"):
                notifier.update_urls(settings.apprise_urls)
            if hasattr(notifier, "update_settings"):
                notifier.update_settings(settings)
        logger.info(f"[notifier/composite] updated settings (cooldown={self._cooldown}s)")

    # ── Core ──────────────────────────────────────────────────────────────────

    async def notify(self, event: EventRecord, cooldown_seconds: int | None = None) -> bool:
        if not self._notifiers or not event.objects:
            return True


        camera = event.camera_name or "unknown"
        now = time.monotonic()
        last = self._last_notified.get(camera, 0.0)
        elapsed = now - last

        effective_cooldown = cooldown_seconds if cooldown_seconds is not None else self._cooldown

        if elapsed < effective_cooldown:
            remaining = int(effective_cooldown - elapsed)
            logger.debug(
                f"[notifier/composite] cooldown active for '{camera}': "
                f"{remaining}s remaining — skipping"
            )
            return False

        self._last_notified[camera] = now

        for notifier in self._notifiers:
            try:
                await notifier.notify(event)
            except Exception as exc:
                logger.error(
                    f"[notifier/composite] {type(notifier).__name__} raised: {exc}",
                    exc_info=True,
                )
        return True

