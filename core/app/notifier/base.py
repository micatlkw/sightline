from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.event import EventRecord


class BaseNotifier(ABC):
    """Abstract base for all notification backends."""

    @abstractmethod
    async def notify(self, event: EventRecord) -> None:
        """Send a notification for *event*."""
        ...

    async def startup(self) -> None:
        """Called once at application startup.  Override to initialise connections."""

    async def shutdown(self) -> None:
        """Called at application shutdown.  Override to clean up resources."""
