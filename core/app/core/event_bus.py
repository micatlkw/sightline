import asyncio
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class EventBus:
    """
    In-process asyncio pub/sub bus.

    Each subscriber gets its own independent asyncio.Queue, so a slow
    consumer (e.g. a WebSocket client with a slow connection) cannot block
    other subscribers or the pipeline.

    Topics used by sightline-core:
      "detection"  — a new EventRecord has been saved (payload: event.to_dict())
      "clip.done"  — a clip finished processing  (payload: {"path": "..."})
      "clip.error" — a clip failed               (payload: {"path": "...", "error": "..."})
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, topic: str, maxsize: int = 100) -> asyncio.Queue:
        """Return a new queue that will receive all future *topic* events."""
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs[topic].append(q)
        logger.debug(
            f"[bus] subscribe topic='{topic}' "
            f"total={len(self._subs[topic])}"
        )
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove *queue* from all topics."""
        for topic, queues in self._subs.items():
            if queue in queues:
                queues.remove(queue)
                logger.debug(f"[bus] unsubscribe topic='{topic}'")
                return

    def publish(self, topic: str, payload: dict) -> None:
        """
        Push *payload* to all subscribers on *topic*.
        Non-blocking: full queues are warned about and the event is dropped
        for that subscriber (prevents one slow client from stalling the pipeline).
        """
        queues = self._subs.get(topic, [])
        for q in queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning(f"[bus] queue full on '{topic}' — dropping event for one subscriber")

        if queues:
            logger.debug(f"[bus] published to '{topic}' ({len(queues)} subscriber(s))")
