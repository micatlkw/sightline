from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable, Optional

import yaml

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.config import Settings

logger = logging.getLogger(__name__)


class _SettingsFileHandler(FileSystemEventHandler):
    def __init__(self, target_filename: str, on_changed: Callable[[], None]) -> None:
        self._target_filename = target_filename
        self._on_changed = on_changed

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and Path(event.src_path).name == self._target_filename:
            self._on_changed()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and Path(event.src_path).name == self._target_filename:
            self._on_changed()

    def on_moved(self, event: FileSystemEvent) -> None:
        dest = getattr(event, "dest_path", None)
        if dest and Path(dest).name == self._target_filename:
            self._on_changed()


class SettingsWatcher:
    """
    Watches /config/settings.json (or specified config path) for filesystem modifications
    and hot-reloads settings into the running application with debouncing.
    """

    def __init__(
        self,
        config_path: Path,
        on_reloaded: Callable[[Settings], None],
        debounce_seconds: float = 0.5,
    ) -> None:
        self._config_path = config_path
        self._on_reloaded = on_reloaded
        self._debounce_seconds = debounce_seconds
        self._observer: Optional[Observer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._debounce_task: Optional[asyncio.TimerHandle] = None
        self._last_mtime: float = 0.0

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        watch_dir = self._config_path.parent

        try:
            watch_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"[settings_watcher] could not create watch dir {watch_dir}: {e}")

        if not watch_dir.exists():
            logger.warning(f"[settings_watcher] watch dir {watch_dir} does not exist — watcher inactive")
            return

        if self._config_path.exists():
            try:
                self._last_mtime = self._config_path.stat().st_mtime
            except Exception:
                pass

        handler = _SettingsFileHandler(
            target_filename=self._config_path.name,
            on_changed=self._schedule_reload,
        )

        self._observer = Observer()
        self._observer.schedule(handler, str(watch_dir), recursive=False)
        self._observer.start()
        logger.info(f"[settings_watcher] watching {self._config_path} for dynamic changes")

    async def stop(self) -> None:
        if self._debounce_task:
            self._debounce_task.cancel()
            self._debounce_task = None

        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join()
            logger.info("[settings_watcher] stopped")

    def _schedule_reload(self) -> None:
        if not self._loop or self._loop.is_closed():
            return

        if self._debounce_task:
            self._debounce_task.cancel()

        self._debounce_task = self._loop.call_later(
            self._debounce_seconds,
            self._trigger_reload_sync,
        )

    def _trigger_reload_sync(self) -> None:
        if not self._loop or self._loop.is_closed():
            return
        asyncio.create_task(self.reload())

    async def reload(self) -> Optional[Settings]:
        """Reads config file, validates settings, and invokes on_reloaded callback."""
        if not self._config_path.is_file():
            logger.debug(f"[settings_watcher] {self._config_path} does not exist — skipping reload")
            return None

        try:
            mtime = self._config_path.stat().st_mtime
            if mtime == self._last_mtime:
                return None
            self._last_mtime = mtime

            text = self._config_path.read_text(encoding="utf-8")
            data = None
            try:
                data = yaml.safe_load(text)
            except Exception:
                data = json.loads(text)

            if not isinstance(data, dict):
                logger.warning(f"[settings_watcher] {self._config_path} does not contain a valid mapping/object")
                return None

            # Dynamically reload app.config module if modified on disk
            import importlib, sys
            if "app.config" in sys.modules:
                try:
                    importlib.reload(sys.modules["app.config"])
                except Exception as exc:
                    logger.debug(f"[settings_watcher] config module reload error: {exc}")

            from app.config import Settings
            base_settings = Settings()
            new_settings = base_settings.update_with(data)

            logger.info(f"[settings_watcher] successfully loaded and validated {self._config_path}")
            if self._on_reloaded:
                self._on_reloaded(new_settings)
            return new_settings
        except Exception as exc:
            logger.warning(f"[settings_watcher] error parsing/validating {self._config_path}: {exc}")

        return None
