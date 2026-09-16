from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.config import Settings
from app.core.helpers import is_valid_mp4

logger = logging.getLogger(__name__)


# ── Watchdog event handler ────────────────────────────────────────────────────


class _Mp4Handler(FileSystemEventHandler):
    """
    Forwards new .mp4 files to an asyncio Queue.
    Handles both on_created (direct write) and on_moved (write-then-rename,
    which many camera bridges use).
    """

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        self._queue = queue
        self._loop = loop

    def on_created(self, event) -> None:  # type: ignore[override]
        if not event.is_directory and event.src_path.lower().endswith(".mp4"):
            path = Path(event.src_path)
            logger.info(f"[watcher] created: {path.name}")
            self._enqueue(path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        # Some bridges write to a .tmp file then atomically rename → .mp4
        if not event.is_directory and event.dest_path.lower().endswith(".mp4"):
            path = Path(event.dest_path)
            logger.info(f"[watcher] renamed → {path.name}")
            self._enqueue(path)

    def _enqueue(self, path: Path) -> None:
        # call_soon_threadsafe is required because watchdog runs in its own
        # thread, while the asyncio queue lives in the event loop thread.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (path, time.time()))


# ── Watcher ───────────────────────────────────────────────────────────────────


class DirectoryWatcher:
    """
    Monitors *watch_dir* for new .mp4 files and pushes their paths onto
    *clip_queue*.

    Backend:
      - InotifyObserver  (Linux, default) — instant, zero-latency.
      - PollingObserver  (fallback)       — set WATCH_USE_POLLING=true when
        the watch directory is an NFS/SMB network mount.
    """

    def __init__(self, settings: Settings, clip_queue: asyncio.Queue) -> None:
        self._watch_dir = settings.incoming_dir
        self._stable_secs = settings.clip_stable_seconds
        self._queue = clip_queue
        self._use_polling = settings.watch_use_polling
        self._observer: Observer | PollingObserver | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        handler = _Mp4Handler(self._queue, loop)

        if self._use_polling:
            logger.info("[watcher] mode=polling (PollingObserver)")
            self._observer = PollingObserver()
        else:
            logger.info("[watcher] mode=inotify (InotifyObserver)")
            self._observer = Observer()

        self._observer.schedule(handler, str(self._watch_dir), recursive=True)
        self._observer.start()
        logger.info(f"[watcher] watching {self._watch_dir}")

    async def stop(self) -> None:
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join()
            logger.info("[watcher] stopped")

    def update_settings(self, settings: Settings) -> None:
        """Dynamically updates stability wait duration."""
        self._stable_secs = settings.clip_stable_seconds
        logger.info(f"[watcher] settings updated: clip_stable_seconds={self._stable_secs}")

    async def wait_for_stable(self, path: Path) -> bool:
        """
        Ensure *path* is completely written.
        - Fast path: If the file is a structurally complete, finalized MP4 (moov atom at EOF),
          returns True immediately in < 1ms with zero sleep delay.
        - If the file has not been modified for at least *clip_stable_seconds*, returns True immediately.
        - Otherwise, polls file size every 0.5s until unchanging or until container finalizes.
        """
        try:
            stat = path.stat()
            # Instant validation for finalized MP4 files (atomic renames from arlo_sync)
            if stat.st_size >= 32 and path.suffix.lower() == ".mp4":
                valid, _ = is_valid_mp4(path)
                if valid:
                    logger.debug(f"[watcher] {path.name} verified as finalized MP4 ({stat.st_size:,} bytes) — zero delay")
                    return True

            # If the file hasn't been written to for >= stable_secs, it's already complete
            if stat.st_size > 0 and (time.time() - stat.st_mtime) >= self._stable_secs:
                logger.debug(f"[watcher] {path.name} is already stable ({stat.st_size} bytes)")
                return True
        except FileNotFoundError:
            logger.warning(f"[watcher] {path.name} not found during stability check")
            return False
        except Exception:
            pass

        required_checks = max(2, int(self._stable_secs / 0.5))
        prev_size = -1
        stable_count = 0
        max_iterations = int(max(60.0, self._stable_secs * 15) / 0.5)

        for _ in range(max_iterations):
            try:
                stat = path.stat()
                size = stat.st_size
                # Check if file finalized during the wait
                if size >= 32 and path.suffix.lower() == ".mp4":
                    valid, _ = is_valid_mp4(path)
                    if valid:
                        logger.debug(f"[watcher] {path.name} finalized during stability poll ({size:,} bytes)")
                        return True
            except FileNotFoundError:
                logger.warning(f"[watcher] {path.name} disappeared during stability check")
                return False

            if size > 0 and size == prev_size:
                stable_count += 1
                if stable_count >= required_checks:
                    logger.debug(f"[watcher] {path.name} stable at {size} bytes")
                    return True
            else:
                stable_count = 0

            prev_size = size
            await asyncio.sleep(0.5)

        logger.warning(f"[watcher] {path.name} stability timeout")
        return False

    @property
    def is_alive(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    @property
    def mode(self) -> str:
        return "polling" if self._use_polling else "inotify"
