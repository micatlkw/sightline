from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.core.event_bus import EventBus
from app.core.helpers import (
    extract_camera_name,
    extract_clip_datetime,
    extract_date_str,
    format_detection_suffix,
    format_duration,
    get_mp4_duration,
)
from app.database import Database
from app.detector.yolo_detector import YoloDetector
from app.models.event import DetectionItem, EventRecord
from app.notifier.base import BaseNotifier
from app.watcher.directory_watcher import DirectoryWatcher

logger = logging.getLogger(__name__)


class Pipeline:
    """
    Orchestrates the full surveillance pipeline:

      [DirectoryWatcher / inotify]
          → asyncio.Queue
          → stability check (file fully written)
          → YoloDetector (in ThreadPoolExecutor, 1 worker)
          → Database.save_event
          → EventBus.publish("detection", …)
          → CompositeNotifier.notify
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        detector: YoloDetector,
        watcher: DirectoryWatcher,
        notifier: BaseNotifier,
        bus: EventBus,
        clip_queue: asyncio.Queue,
    ) -> None:
        self._settings = settings
        self._incoming_dir = settings.incoming_dir
        self._processed_dir = settings.processed_dir
        self._db = db
        self._detector = detector
        self._watcher = watcher
        self._notifier = notifier
        self._bus = bus
        self._clip_queue = clip_queue

        # Concurrent worker executors: configured via pipeline_concurrency setting
        self._concurrency = max(1, int(getattr(settings, "pipeline_concurrency", 2) or 2))
        self._executor = ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="yolo")
        self._thumb_executor = ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="thumb")
        self._worker_tasks: list[asyncio.Task] = []
        self._task: asyncio.Task | None = None

        # Stats exposed via /health
        self.processed: int = 0
        self.errors: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._watcher.start()
        await self._prewarm_workers()
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(i), name=f"pipeline_worker_{i}")
            for i in range(self._concurrency)
        ]
        if self._worker_tasks:
            self._task = self._worker_tasks[0]
        if self._settings.scan_on_startup:
            queued = await self.scan_and_enqueue()
            if queued:
                logger.info(f"[pipeline] enqueued {len(queued)} clip(s) from startup scan")
        logger.info(f"[pipeline] started with {self._concurrency} concurrent worker(s)")

    async def _prewarm_workers(self) -> None:
        """Initializes and warms up thread-local YOLO models across all worker threads during startup."""
        try:
            loop = asyncio.get_running_loop()
            def _warmup_thread():
                if hasattr(self._detector, "_get_model"):
                    self._detector._get_model()
            futs = [loop.run_in_executor(self._executor, _warmup_thread) for _ in range(self._concurrency)]
            await asyncio.gather(*futs, return_exceptions=True)
            logger.info(f"[pipeline] pre-warmed {self._concurrency} inference worker thread(s)")
        except Exception as e:
            logger.warning(f"[pipeline] worker pre-warm warning: {e}")

    async def stop(self) -> None:
        await self._watcher.stop()
        for task in self._worker_tasks:
            if not task.done():
                task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self._task = None
        self._executor.shutdown(wait=False)
        self._thumb_executor.shutdown(wait=False)
        logger.info("[pipeline] stopped")

    def update_settings(self, settings: Settings) -> None:
        """Propagates updated settings to detector, notifier, watcher, and internal state."""
        self._settings = settings
        self._incoming_dir = settings.incoming_dir
        self._processed_dir = settings.processed_dir
        new_concurrency = max(1, int(getattr(settings, "pipeline_concurrency", 2) or 2))
        if new_concurrency != self._concurrency:
            logger.info(f"[pipeline] pipeline concurrency setting changed: {self._concurrency} -> {new_concurrency}")
            self._concurrency = new_concurrency
        if hasattr(self._detector, "update_settings"):
            self._detector.update_settings(settings)
        if hasattr(self._notifier, "update_settings"):
            self._notifier.update_settings(settings)
        if hasattr(self._watcher, "update_settings"):
            self._watcher.update_settings(settings)
        logger.info("[pipeline] settings updated across components")

    # ── Manual enqueue / Backlog scan ─────────────────────────────────────────

    async def scan_and_enqueue(self) -> list[Path]:
        """
        Scans incoming_dir recursively for .mp4 clips not yet marked 'done' in the database,
        sorts them chronologically (oldest first), and enqueues them for processing.
        """
        if not self._incoming_dir.exists():
            return []

        candidates = [
            p for p in self._incoming_dir.rglob("*")
            if p.is_file() and p.suffix.lower() == ".mp4"
        ]

        if not candidates:
            return []

        done_paths = await self._db.get_done_clip_paths()

        unprocessed = [
            p for p in candidates
            if str(p) not in done_paths and str(p.resolve()) not in done_paths
        ]

        def _get_mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        unprocessed.sort(key=lambda p: (_get_mtime(p), str(p)))

        enqueued: list[Path] = []
        for clip_path in unprocessed:
            await self.enqueue(clip_path)
            enqueued.append(clip_path)

        if enqueued:
            logger.info(f"[pipeline] scan discovered and enqueued {len(enqueued)} clip(s)")
        return enqueued

    async def enqueue(self, clip_path: Path) -> None:
        """Manually push a clip path into the processing queue."""
        await self._db.mark_clip(clip_path, "pending")
        await self._clip_queue.put((clip_path, time.time()))
        self._bus.publish("clip.queued", {"path": clip_path.name, "filename": clip_path.name})
        logger.info(f"[pipeline] manually enqueued: {clip_path.name}")

    # ── Processing loop ───────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Legacy single-worker loop fallback."""
        await self._worker_loop(0)

    async def _worker_loop(self, worker_id: int) -> None:
        logger.info(f"[pipeline] worker #{worker_id} running")
        while True:
            item = await self._clip_queue.get()
            if isinstance(item, tuple) and len(item) == 2:
                clip_path, enqueued_at = item
            else:
                clip_path = item
                enqueued_at = time.time()
            try:
                await self._process(clip_path, enqueued_at)
            except Exception as e:
                logger.error(f"[pipeline] worker #{worker_id} unhandled exception on {clip_path}: {e}", exc_info=True)
            finally:
                try:
                    self._clip_queue.task_done()
                except ValueError:
                    pass

    async def _process(self, clip_path: Path, enqueued_at: float | None = None) -> None:
        process_start = time.perf_counter()
        enqueued_ts = enqueued_at if enqueued_at is not None else time.time()
        queue_wait = max(0.0, time.time() - enqueued_ts)
        clip_recorded_dt = extract_clip_datetime(clip_path)
        rec_start_ts = clip_recorded_dt.timestamp()
        video_dur = get_mp4_duration(clip_path)
        rec_end_ts = rec_start_ts + video_dur
        sync_lag = max(0.0, enqueued_ts - rec_end_ts)
        camera_name: str | None = None

        logger.info(f"[pipeline] → {clip_path.name} (queued {queue_wait:.2f}s ago)")

        # Wait until the camera has finished writing the file
        stable = await self._watcher.wait_for_stable(clip_path)
        if not stable:
            elapsed = time.perf_counter() - process_start
            logger.warning(f"[pipeline] {clip_path.name} never stabilised (after {elapsed:.2f}s) — moving to corrupt")
            final_path, camera_name = self._move_to_corrupt(clip_path)
            await self._db.mark_clip(clip_path, "error", "file never stabilised", target_path=final_path)
            self._bus.publish(
                "clip.error",
                {
                    "path": final_path.name,
                    "filename": final_path.name,
                    "camera_name": camera_name,
                    "error": "file never stabilised",
                },
            )
            self.errors += 1
            return

        # Resolve camera name and check if disabled
        camera_mapping = self._settings.get_camera_mapping()
        camera_name = extract_camera_name(
            clip_path,
            self._incoming_dir,
            custom_mapping=camera_mapping,
        )

        if not self._settings.is_camera_enabled(camera_name):
            logger.info(f"[pipeline] ⏭️ {clip_path.name} (camera: {camera_name}): camera is disabled in settings.yaml — skipping detection")
            final_path, _ = self._move_to_processed(clip_path, [], camera_name=camera_name)
            await self._db.mark_clip(clip_path, "done", target_path=final_path)
            self._bus.publish(
                "clip.done",
                {
                    "path": str(final_path),
                    "filename": final_path.name,
                    "camera_name": camera_name,
                    "skipped": True,
                },
            )
            self.processed += 1
            return

        # Resolve per-camera target classes and confidence threshold
        target_classes = self._settings.get_camera_target_classes(camera_name)
        conf_threshold = self._settings.get_camera_confidence_threshold(camera_name)
        class_conf_thresholds = (
            self._settings.get_camera_class_confidence_thresholds(camera_name)
            if hasattr(self._settings, "get_camera_class_confidence_thresholds")
            else None
        )
        camera_cooldown = self._settings.get_camera_cooldown_seconds(camera_name)

        loop = asyncio.get_running_loop()
        early_event_info: dict[str, Any] = {}
        early_alert_fut: asyncio.Future | None = None
        infer_start = time.perf_counter()

        async def _dispatch_early_alert(
            early_results: list[DetectionItem],
            thumb_path: str | None,
            timestamp_sec: float,
            frame_idx: int = 1,
        ) -> None:
            nonlocal early_event_info
            try:
                alert_start = time.perf_counter()
                early_event = await self._db.save_event(clip_path, early_results, camera_name=camera_name)
                early_event_info["event"] = early_event
                early_event_info["alert_ts"] = time.time()
                early_event_info["early_time"] = time.perf_counter() - infer_start
                early_event_info["frame_idx"] = frame_idx
                early_event_info["timestamp_sec"] = timestamp_sec
                self._bus.publish("detection", early_event.to_dict())

                notify_res = await self._notifier.notify(early_event, cooldown_seconds=camera_cooldown)
                sent = notify_res is not False
                early_event_info["sent"] = sent
                if sent:
                    await self._db.mark_notified(early_event.id)
                early_event_info["notify_elapsed"] = time.perf_counter() - alert_start
            except Exception as ex:
                logger.warning(f"[pipeline] early alert dispatch error for {clip_path.name}: {ex}", exc_info=True)

        def _on_early_detection(
            early_results: list[DetectionItem],
            early_frame: np.ndarray,
            timestamp_sec: float,
            frame_idx: int = 1,
            *args,
        ) -> None:
            nonlocal early_alert_fut
            try:
                # Save snapshot thumbnail synchronously in the worker thread (< 2ms)
                thumb_path = self._detector._save_snapshot_thumbnail(
                    early_frame, clip_path, early_results, camera_name=camera_name
                )
                if thumb_path:
                    for d in early_results:
                        d.keyframe_path = thumb_path

                early_alert_fut = asyncio.run_coroutine_threadsafe(
                    _dispatch_early_alert(early_results, thumb_path, timestamp_sec, frame_idx),
                    loop,
                )
            except Exception as ex:
                logger.debug(f"[pipeline] early detection callback error: {ex}")

        try:
            # 1. Run blocking YOLO inference in the thread pool with per-camera overrides
            detection_res = await loop.run_in_executor(
                self._executor,
                self._detector.detect_sync,
                clip_path,
                target_classes,
                conf_threshold,
                camera_name,
                class_conf_thresholds,
                True,  # request frames for async GIF rendering
                _on_early_detection,
            )

            if isinstance(detection_res, tuple) and len(detection_res) == 2:
                detections, collected_frames = detection_res
            else:
                detections = detection_res
                collected_frames = []

            infer_elapsed = time.perf_counter() - infer_start

            # Ensure early alert task has fully resolved if triggered
            if early_alert_fut is not None:
                try:
                    await asyncio.wrap_future(early_alert_fut)
                except Exception as ex:
                    logger.debug(f"[pipeline] awaiting early alert error: {ex}")

            # 2. Move clip from incoming_dir to processed_dir with formatted name and subfolders
            final_path, camera_name = self._move_to_processed(clip_path, detections, camera_name=camera_name)
            elapsed_total = time.perf_counter() - process_start

            if early_event_info.get("event"):
                event: EventRecord = early_event_info["event"]
                # Update DB with final clip destination and full detection list
                await self._db.update_event_clip_path(event.id, str(final_path))
                if len(detections) != len(event.objects):
                    await self._db.update_event_objects(event.id, detections)
                event.clip_path = str(final_path)

                alert_ts = early_event_info.get("alert_ts", time.time())
                early_time = early_event_info.get("early_time", 0.0)
                frame_idx = early_event_info.get("frame_idx", 1)
                early_ts = early_event_info.get("timestamp_sec", 0.0)
                early_detail = f"(frame {frame_idx} @ {early_ts:.1f}s)" if early_ts > 0 else f"(frame {frame_idx})"
                sent = early_event_info.get("sent", False)
                delay_seconds = max(0.0, alert_ts - rec_start_ts)
                delay_str = format_duration(delay_seconds)
                status_note = (
                    f"notification delay: {delay_str}"
                    if sent
                    else f"notification: cooldown active, delay: {delay_str}"
                )
                det_summary = ", ".join(f"{d.class_name} ({d.confidence:.0%})" for d in detections)
                logger.info(
                    f"[pipeline] ✓ {final_path.name} (camera: {event.camera_name}): "
                    f"{len(detections)} detection(s) [{det_summary}] → event #{event.id} ({status_note})\n"
                    f"           └─ Latency Breakdown ({delay_str} alert delay):\n"
                    f"              • Camera Recording           : {video_dur:.1f}s\n"
                    f"              • Camera Finalize & Sync Lag : {sync_lag:.2f}s (camera EOF -> NAS arrival)\n"
                    f"              • Queue Wait                 : {queue_wait:.2f}s\n"
                    f"              • Early Alert                : {early_time:.2f}s {early_detail}\n"
                    f"              • Total Inference            : {infer_elapsed:.2f}s ({len(collected_frames)} frames)"
                )

                # Launch non-blocking background task to render full-clip animated GIF
                if collected_frames and event.id and hasattr(self._detector, "render_full_clip_gif"):
                    asyncio.create_task(
                        self._async_generate_full_gif(event.id, collected_frames, final_path, detections, camera_name),
                        name=f"gif_{event.id}",
                    )

            elif detections:
                event = await self._db.save_event(final_path, detections, camera_name=camera_name)
                self._bus.publish("detection", event.to_dict())

                notify_res = await self._notifier.notify(event, cooldown_seconds=camera_cooldown)
                sent = notify_res is not False
                if sent:
                    await self._db.mark_notified(event.id)

                notified_at = datetime.now().astimezone()
                delay_seconds = (notified_at - clip_recorded_dt).total_seconds()
                delay_str = format_duration(delay_seconds)
                status_note = (
                    f"notification delay: {delay_str}"
                    if sent
                    else f"notification: cooldown active, delay: {delay_str}"
                )
                det_summary = ", ".join(f"{obj['class']} ({obj['confidence']:.0%})" for obj in event.objects)
                logger.info(
                    f"[pipeline] ✓ {final_path.name} (camera: {event.camera_name}): "
                    f"{len(detections)} detection(s) [{det_summary}] → event #{event.id} ({status_note})\n"
                    f"           └─ Latency Breakdown ({delay_str} alert delay):\n"
                    f"              • Camera Recording           : {video_dur:.1f}s\n"
                    f"              • Camera Finalize & Sync Lag : {sync_lag:.2f}s (camera EOF -> NAS arrival)\n"
                    f"              • Queue Wait                 : {queue_wait:.2f}s\n"
                    f"              • Total Inference            : {infer_elapsed:.2f}s ({len(collected_frames)} frames)"
                )

                # Launch non-blocking background task to render full-clip animated GIF
                if collected_frames and event.id and hasattr(self._detector, "render_full_clip_gif"):
                    asyncio.create_task(
                        self._async_generate_full_gif(event.id, collected_frames, final_path, detections, camera_name),
                        name=f"gif_{event.id}",
                    )
            else:
                event = await self._db.save_event(final_path, detections, camera_name=camera_name)
                logger.info(
                    f"[pipeline] ✓ {final_path.name} (camera: {camera_name}): "
                    f"no qualifying detections → event #{event.id}\n"
                    f"           └─ Latency Breakdown (total: {elapsed_total:.2f}s):\n"
                    f"              • Camera Recording           : {video_dur:.1f}s\n"
                    f"              • Camera Finalize & Sync Lag : {sync_lag:.2f}s (camera EOF -> NAS arrival)\n"
                    f"              • Queue Wait                 : {queue_wait:.2f}s\n"
                    f"              • Total Inference            : {infer_elapsed:.2f}s ({len(collected_frames)} frames)"
                )

            await self._db.mark_clip(clip_path, "done", target_path=final_path)
            self._bus.publish(
                "clip.done",
                {
                    "path": str(final_path),
                    "filename": final_path.name,
                    "camera_name": camera_name,
                },
            )
            self.processed += 1

        except Exception as exc:
            elapsed_total = time.perf_counter() - process_start
            logger.error(
                f"[pipeline] ✗ {clip_path.name}: {exc} (after {elapsed_total:.2f}s)",
                exc_info=True,
            )
            final_path, camera_name = self._move_to_corrupt(clip_path, camera_name=camera_name)
            await self._db.mark_clip(clip_path, "error", str(exc), target_path=final_path)
            self._bus.publish(
                "clip.error",
                {
                    "path": str(final_path),
                    "filename": final_path.name,
                    "camera_name": camera_name,
                    "error": str(exc),
                },
            )
            self.errors += 1

    def _move_clip(self, clip_path: Path, target_dir: Path, filename: str | None = None) -> Path:
        """
        Moves clip from incoming_dir to target_dir/filename,
        creating directories if needed and handling name collisions.
        """
        try:
            if not clip_path.exists():
                return clip_path

            dest_filename = filename or clip_path.name
            target_dir.mkdir(parents=True, exist_ok=True)
            dest_path = target_dir / dest_filename

            if dest_path.resolve() == clip_path.resolve():
                return clip_path

            # Avoid collisions if file already exists at destination
            if dest_path.exists():
                stem = dest_path.stem
                suffix = dest_path.suffix
                timestamp = int(time.time())
                dest_path = dest_path.parent / f"{stem}_{timestamp}{suffix}"

            import shutil
            shutil.move(str(clip_path), str(dest_path))
            logger.info(f"[pipeline] moved clip: {clip_path.name} → {dest_path}")
            return dest_path
        except Exception as exc:
            logger.warning(f"[pipeline] could not move {clip_path} to {target_dir}: {exc}")
            return clip_path

    def _move_to_processed(self, clip_path: Path, detections: list[DetectionItem], camera_name: str | None = None) -> tuple[Path, str]:
        resolved_camera = camera_name or extract_camera_name(
            clip_path,
            self._incoming_dir,
            custom_mapping=self._settings.get_camera_mapping(),
        )
        date_str = extract_date_str(clip_path)
        suffix_str = format_detection_suffix(detections)
        dest_filename = f"{clip_path.stem}{suffix_str}{clip_path.suffix}"
        target_dir = self._processed_dir / date_str / resolved_camera
        dest_path = self._move_clip(clip_path, target_dir, dest_filename)
        return dest_path, resolved_camera

    def _move_to_corrupt(self, clip_path: Path, camera_name: str | None = None) -> tuple[Path, str]:
        resolved_camera = camera_name or extract_camera_name(
            clip_path,
            self._incoming_dir,
            custom_mapping=self._settings.get_camera_mapping(),
        )
        date_str = extract_date_str(clip_path)
        dest_filename = clip_path.name
        target_dir = self._processed_dir / "corrupt" / date_str / resolved_camera
        dest_path = self._move_clip(clip_path, target_dir, dest_filename)
        return dest_path, resolved_camera

    async def _async_generate_full_gif(
        self,
        event_id: int,
        frames: list,
        clip_path: Path,
        detections: list[DetectionItem],
        camera_name: str | None = None,
    ) -> None:
        """
        Asynchronously renders the full-clip animated GIF preview in the thumbnail executor,
        updates the event thumbnail in the database, and notifies clients via WebSocket.
        """
        try:
            loop = asyncio.get_running_loop()
            gif_path = await loop.run_in_executor(
                self._thumb_executor,
                self._detector.render_full_clip_gif,
                frames,
                clip_path,
                detections,
                camera_name,
            )
            if gif_path:
                await self._db.update_event_thumbnail(event_id, gif_path)
                self._bus.publish(
                    "event.thumbnail_ready",
                    {
                        "id": event_id,
                        "thumbnail": gif_path,
                        "filename": Path(gif_path).name,
                        "camera_name": camera_name,
                    },
                )
                logger.info(f"[pipeline] background full-clip GIF ready for event #{event_id}: {Path(gif_path).name}")
        except Exception as exc:
            logger.warning(f"[pipeline] background full-clip GIF generation error for event #{event_id}: {exc}")
