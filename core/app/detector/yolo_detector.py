from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image
from ultralytics import YOLO

from app.config import Settings
from app.core.helpers import extract_camera_name, extract_date_str, format_detection_suffix
from app.models.event import DetectionItem

logger = logging.getLogger(__name__)

# Subset of COCO names relevant to surveillance
_COCO_NAMES: dict[int, str] = {
    0: "person",
    2: "car",
    3: "motorcycle",
    7: "truck",
    15: "cat",
    16: "dog",
    21: "bear",
}

# Verified SHA-256 digests for official Ultralytics weights
OFFICIAL_YOLO_HASHES: dict[str, set[str]] = {
    "yolo26n.pt": {"9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"},
    "yolo26s.pt": {"646f8bc3fe0a656803d95c294f7852321748cb29d13466a1af8862e2db384a1b"},
    "yolo26m.pt": {"401cea9ab23ad19246ff7744859816bc599f350e93c9dd30367b6f0a0745d0b7"},
    "yolo11n.pt": {"0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1"},
    "yolo11s.pt": {"85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5"},
    "yolo11m.pt": {"d5ffc1a674953a08e11a8d21e022781b1b23a19b730afc309290bd9fb5305b95"},
    "yolov8n.pt": {
        "f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36",
        "31e20dde3def09e2cf938c7be6fe23d9150bbbe503982af13345706515f2ef95",
    },
    "yolov8s.pt": {
        "1f47a78bf100391c2a140b7ac73a1caae18c32779be7d310658112f7ac9aa78a",
        "268e5bb54c640c96c3510224833bc2eeacab4135c6deb41502156e39986b562d",
    },
    "yolov8m.pt": {
        "5d4a90cdc7a21786cc59cd19778e9eafff836df9e2da32524737c7ee6efe4fe5",
        "6c25b0b63b1a433843f06d821a9ac1deb8d5805f74f0f38772c7308c5adc55a5",
    },
}


def compute_file_sha256(path: Path) -> str:
    """Computes SHA-256 hex digest for a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


class YoloDetector:
    """
    CPU-optimised YOLO object detector.

    *detect_sync* is a blocking, synchronous method designed to be called
    inside a ThreadPoolExecutor so it doesn't block the asyncio event loop.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conf_threshold = settings.confidence_threshold
        self._vid_stride = settings.vid_stride
        self._target_classes = settings.get_resolved_target_classes()
        self._thumbnails_dir = settings.thumbnails_dir
        self._incoming_dir = settings.incoming_dir
        self._cameras_config_path = settings.cameras_config_path
        try:
            self._thumbnails_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not create thumbnails directory {self._thumbnails_dir}: {e}")

        try:
            settings.models_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not create models directory {settings.models_dir}: {e}")

        # Configure ultralytics weights directory
        try:
            from ultralytics import settings as ultralytics_settings
            ultralytics_settings.update({"weights_dir": str(settings.models_dir)})
        except Exception as e:
            logger.debug(f"[detector] could not update ultralytics weights_dir: {e}")

        self._models_dir = settings.models_dir
        self._current_model_name = settings.yolo_model
        self._current_model_sha256 = settings.yolo_model_sha256
        self._thread_local = threading.local()
        model_path = self._resolve_or_download_model(
            settings.models_dir, settings.yolo_model, settings.yolo_model_sha256
        )
        self._model = YOLO(str(model_path))

        # Optimize CPU threads for inference (prevents core thrashing on hybrid CPUs)
        try:
            import torch
            import os
            n_threads = min(4, os.cpu_count() or 4)
            torch.set_num_threads(n_threads)
            logger.debug(f"[detector] PyTorch threads set to {n_threads}")
        except Exception:
            pass

        # Warm up the model during startup so the first clip processes at full speed
        self._warmup()
        logger.info(f"[detector] YOLO model ready (path: {model_path})")

    def _get_model(self) -> YOLO:
        """
        Returns a thread-isolated YOLO instance to guarantee 100% thread safety
        during concurrent multi-worker inference across CPU cores.
        """
        model_name = getattr(self, "_current_model_name", None)
        if (
            not hasattr(self._thread_local, "model")
            or getattr(self._thread_local, "model_name", None) != model_name
        ):
            model_path = self._resolve_or_download_model(
                self._models_dir, model_name, self._current_model_sha256
            )
            self._thread_local.model = YOLO(str(model_path))
            self._thread_local.model_name = model_name
            # Warm up thread-local instance
            try:
                dummy = np.zeros((640, 640, 3), dtype=np.uint8)
                self._thread_local.model(dummy, verbose=False)
            except Exception:
                pass
        return self._thread_local.model

    def update_settings(self, settings: Settings) -> None:
        """Dynamically updates confidence threshold, vid_stride, target_classes, model, and camera mappings."""
        self._settings = settings
        self._conf_threshold = settings.confidence_threshold
        self._vid_stride = settings.vid_stride
        self._target_classes = settings.get_resolved_target_classes()
        model_changed = getattr(self, "_current_model_name", None) != settings.yolo_model
        sha_changed = getattr(self, "_current_model_sha256", None) != settings.yolo_model_sha256
        if model_changed or sha_changed:
            logger.info(f"[detector] changing YOLO model from {self._current_model_name} to {settings.yolo_model}")
            self._current_model_name = settings.yolo_model
            self._current_model_sha256 = settings.yolo_model_sha256
            self._thread_local = threading.local()  # Reset thread-local models on change
            model_path = self._resolve_or_download_model(
                settings.models_dir, settings.yolo_model, settings.yolo_model_sha256
            )
            self._model = YOLO(str(model_path))
            self._warmup()
            logger.info(f"[detector] new YOLO model ready (path: {model_path})")
        logger.info(
            f"[detector] settings updated: conf={self._conf_threshold}, "
            f"vid_stride={self._vid_stride}, classes={self._target_classes}"
        )

    def _warmup(self) -> None:
        """Run a single warmup pass on a dummy image to initialize PyTorch JIT and memory pools."""
        try:
            t0 = time.perf_counter()
            dummy = np.zeros((640, 640, 3), dtype=np.uint8)
            self._model(dummy, verbose=False)
            logger.info(f"[detector] model warmed up in {time.perf_counter() - t0:.2f}s")
        except Exception as e:
            logger.debug(f"[detector] warmup notice: {e}")

    @staticmethod
    def _resolve_or_download_model(
        models_dir: Path, yolo_model: str, expected_sha256: str | None = None
    ) -> Path:
        """
        Ensures the requested model file resides in models_dir (/models).
        Performs cryptographic SHA-256 verification against official digests or
        user-configured hash before allowing weights to be loaded into PyTorch.
        """
        model_filename = Path(yolo_model).name
        target_file = models_dir / model_filename
        cwd_candidate = Path(model_filename)

        allowed_hashes: set[str] | None = None
        if expected_sha256 and expected_sha256.strip():
            allowed_hashes = {expected_sha256.strip().lower()}
        elif model_filename in OFFICIAL_YOLO_HASHES:
            allowed_hashes = OFFICIAL_YOLO_HASHES[model_filename]

        def _verify_file(path: Path, is_download: bool = False) -> bool:
            if not path.is_file() or path.stat().st_size <= 1000:
                return False
            h = compute_file_sha256(path)
            if allowed_hashes is not None:
                if h in allowed_hashes:
                    logger.info(f"[detector] verified model {path.name} SHA-256: {h}")
                    return True
                logger.error(
                    f"[detector] SECURITY INTEGRITY ERROR: Model {path.name} SHA-256 {h} "
                    f"does not match expected hash(es): {allowed_hashes}."
                )
                if is_download:
                    path.unlink(missing_ok=True)
                return False
            else:
                if is_download:
                    logger.error(
                        f"[detector] SECURITY REFUSAL: Refusing to download unverified custom model {path.name} "
                        "without an expected SHA-256 hash."
                    )
                    path.unlink(missing_ok=True)
                    return False
                else:
                    logger.warning(
                        f"[detector] SECURITY AUDIT: Loaded pre-existing custom model {path.name} from disk "
                        f"(SHA-256: {h}) without an expected checksum configured."
                    )
                    return True

        # 1. Already exists in models_dir
        if target_file.is_file():
            if _verify_file(target_file, is_download=False):
                logger.info(f"[detector] loading model from volume: {target_file}")
                if cwd_candidate.is_file():
                    cwd_candidate.unlink(missing_ok=True)
                return target_file
            else:
                raise ValueError(
                    f"Model integrity verification failed for {target_file}. "
                    "File SHA-256 does not match official or configured digest."
                )

        # 2. Exists in cwd (/app) from an earlier download -> move and verify
        if cwd_candidate.is_file():
            if _verify_file(cwd_candidate, is_download=False):
                import shutil
                shutil.move(str(cwd_candidate), str(target_file))
                logger.info(f"[detector] moved verified {cwd_candidate} to {target_file}")
                return target_file
            else:
                cwd_candidate.unlink(missing_ok=True)

        # 3. Check if download is permitted
        if allowed_hashes is None:
            raise ValueError(
                f"Refusing to download untrusted model '{model_filename}' without a verified SHA-256 checksum. "
                "Please configure 'yolo_model_sha256' in Settings or choose an official model."
            )

        # 4. Download directly into models_dir
        logger.info(f"[detector] {target_file} not found — downloading to {models_dir}...")
        download_urls = [
            f"https://github.com/ultralytics/assets/releases/download/v8.4.0/{model_filename}",
            f"https://github.com/ultralytics/assets/releases/download/v8.3.0/{model_filename}",
            f"https://github.com/ultralytics/assets/releases/download/v8.2.0/{model_filename}",
            f"https://github.com/ultralytics/assets/releases/download/v0.0.0/{model_filename}",
        ]

        import urllib.request
        downloaded = False
        temp_file = models_dir / f"{model_filename}.tmp"

        for url in download_urls:
            try:
                logger.info(f"[detector] fetching weights from {url}")
                urllib.request.urlretrieve(url, str(temp_file))
                if _verify_file(temp_file, is_download=True):
                    temp_file.replace(target_file)
                    logger.info(f"[detector] successfully saved and verified {model_filename} to {target_file}")
                    downloaded = True
                    break
            except Exception as exc:
                logger.debug(f"[detector] download failed from {url}: {exc}")
                if temp_file.exists():
                    temp_file.unlink(missing_ok=True)

        # 5. Clean up any file that might have landed in cwd
        if cwd_candidate.is_file() and cwd_candidate.resolve() != target_file.resolve():
            try:
                cwd_candidate.unlink(missing_ok=True)
            except Exception:
                pass

        if not target_file.is_file():
            raise FileNotFoundError(
                f"Failed to acquire authentic model weights for '{model_filename}'."
            )

        return target_file

    # ── Public API ────────────────────────────────────────────────────────────

    def detect_sync(
        self,
        clip_path: Path,
        target_classes: list[int] | None = None,
        conf_threshold: float | None = None,
        camera_name: str | None = None,
        class_conf_thresholds: dict[int, float] | None = None,
        return_frames: bool = False,
        on_early_detection: Callable[[list[DetectionItem], np.ndarray, float], None] | None = None,
    ) -> list[DetectionItem] | tuple[list[DetectionItem], list[np.ndarray]]:
        """
        Stream keyframes from *clip_path* at dynamically computed *vid_stride* intervals
        directly through the native Ultralytics pipeline. Returns all qualifying detections across the clip.
        When return_frames=True, returns (results, collected_frames).
        When on_early_detection is provided, fires the callback on the very first detection frame
        without waiting for the entire video to finish streaming.

        Designed to run in a ThreadPoolExecutor — does NOT use asyncio.
        """
        results: list[DetectionItem] = []
        if not clip_path.exists():
            logger.error(f"[detector] file not found: {clip_path}")
            return results

        effective_classes = target_classes if target_classes is not None else self._target_classes
        effective_conf = conf_threshold if conf_threshold is not None else self._conf_threshold

        if class_conf_thresholds is not None:
            effective_class_thresholds = class_conf_thresholds
        elif camera_name and hasattr(self._settings, "get_camera_class_confidence_thresholds"):
            effective_class_thresholds = self._settings.get_camera_class_confidence_thresholds(camera_name)
        else:
            effective_class_thresholds = {}

        candidate_thresholds = [effective_conf]
        if effective_class_thresholds:
            candidate_thresholds.extend(effective_class_thresholds.values())
        pre_filter_conf = min(candidate_thresholds)

        # Probe video readability, FPS, and total frame count quickly for stride and timestamp calculation
        fps = 30.0
        total_frames = 0
        try:
            cap = cv2.VideoCapture(str(clip_path))
            if not cap.isOpened():
                cap.release()
                raise ValueError(
                    f"Failed to open video {clip_path.name}: file is unreadable, corrupted, or missing moov atom"
                )
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            cap.release()
        except ValueError:
            raise
        except Exception as e:
            logger.debug(f"[detector] probe exception for {clip_path.name}: {e}")

        # Resolve stride and sampling FPS dynamically
        if hasattr(self._settings, "resolve_clip_stride_and_fps"):
            effective_stride, effective_fps, is_adapted = self._settings.resolve_clip_stride_and_fps(
                camera_name or "",
                clip_fps=fps,
                total_frames=total_frames,
            )
        else:
            effective_stride = self._vid_stride
            effective_fps = round(fps / effective_stride, 2) if effective_stride > 0 else 1.0
            is_adapted = False

        duration_sec = (total_frames / fps) if (fps > 0 and total_frames > 0) else 0.0
        configured_fps = (
            self._settings.get_camera_sample_fps(camera_name or "")
            if hasattr(self._settings, "get_camera_sample_fps")
            else effective_fps
        )
        fps_info = (
            f"{effective_fps:.1f} FPS (adapted from {configured_fps:.1f} FPS for {duration_sec:.1f}s clip)"
            if is_adapted
            else f"{effective_fps:.1f} FPS"
        )

        # Stream predictions through thread-isolated Ultralytics model instance
        model = self._get_model()
        start_time = time.perf_counter()
        predictions = model(
            str(clip_path),
            classes=effective_classes,
            vid_stride=effective_stride,
            conf=pre_filter_conf,
            stream=True,
            verbose=False,
            imgsz=640,
        )

        keyframe_num = 0
        collected_frames: list[np.ndarray] = []
        best_frame: np.ndarray | None = None
        best_conf: float = -1.0
        early_notified = False

        for result in predictions:
            timestamp_sec = round(keyframe_num * (effective_stride / fps), 2)
            boxes = getattr(result, "boxes", None)

            if boxes is not None and len(boxes) > 0:
                valid_indices: list[int] = []
                for i, box in enumerate(boxes):
                    conf = float(box.conf[0])
                    cls_id = int(box.cls[0])
                    cls_name = self._model.names.get(cls_id) or _COCO_NAMES.get(cls_id, str(cls_id))
                    target_thresh = effective_class_thresholds.get(cls_id, effective_conf)

                    if conf >= target_thresh:
                        valid_indices.append(i)
                    else:
                        logger.debug(
                            f"[detector] {clip_path.name} @ {timestamp_sec:.1f}s: {cls_name} ({conf:.1%} conf) "
                            f"below threshold ({target_thresh:.1%}) — filtered"
                        )

                if valid_indices:
                    try:
                        filtered_result = result[valid_indices]
                        annotated = filtered_result.plot()
                    except Exception:
                        try:
                            annotated = result.plot()
                        except Exception:
                            annotated = getattr(result, "orig_img", None)

                    if annotated is not None:
                        collected_frames.append(annotated)

                    top_box_conf = max((float(boxes[i].conf[0]) for i in valid_indices), default=0.0)
                    if top_box_conf > best_conf:
                        best_conf = top_box_conf
                        best_frame = annotated if annotated is not None else getattr(result, "orig_img", None)

                    for i in valid_indices:
                        box = boxes[i]
                        conf = float(box.conf[0])
                        cls_id = int(box.cls[0])
                        cls_name = self._model.names.get(cls_id) or _COCO_NAMES.get(cls_id, str(cls_id))
                        x1, y1, x2, y2 = box.xyxyn[0].tolist()

                        logger.info(
                            f"[detector] {clip_path.name} @ {timestamp_sec:.1f}s: "
                            f"detected {cls_name.upper()} ({conf:.1%} confidence, bbox=[{x1:.2f}, {y1:.2f}, {x2:.2f}, {y2:.2f}])"
                        )

                        results.append(
                            DetectionItem(
                                class_id=cls_id,
                                class_name=cls_name,
                                confidence=conf,
                                timestamp_sec=timestamp_sec,
                                bbox=[
                                    round(x1, 4),
                                    round(y1, 4),
                                    round(x2, 4),
                                    round(y2, 4),
                                ],
                                keyframe_path=None,
                            )
                        )

                    # Early progressive notification trigger on the very first detection frame
                    if not early_notified and on_early_detection and results:
                        early_notified = True
                        try:
                            early_frame = annotated if annotated is not None else getattr(result, "orig_img", None)
                            if early_frame is not None:
                                on_early_detection(results.copy(), early_frame, timestamp_sec, keyframe_num + 1)
                        except Exception as e:
                            logger.debug(f"[detector] early detection callback notice: {e}")
                else:
                    orig_img = getattr(result, "orig_img", None)
                    if orig_img is not None:
                        collected_frames.append(orig_img)
            else:
                orig_img = getattr(result, "orig_img", None)
                if orig_img is not None:
                    collected_frames.append(orig_img)

            keyframe_num += 1

        elapsed_sec = time.perf_counter() - start_time
        ms_per_kf = (elapsed_sec / keyframe_num * 1000) if keyframe_num > 0 else 0.0
        kfps = (keyframe_num / elapsed_sec) if elapsed_sec > 0 else 0.0

        if results:
            # Save single-frame snapshot immediately for instant notification (< 2ms)
            snapshot_frame = best_frame if best_frame is not None else (collected_frames[0] if collected_frames else None)
            thumb_path = self._save_snapshot_thumbnail(snapshot_frame, clip_path, results, camera_name=camera_name)
            if thumb_path:
                for det in results:
                    det.keyframe_path = thumb_path
            summary = ", ".join(f"{d.class_name} ({d.confidence:.0%})" for d in results)
            logger.info(
                f"[detector] {clip_path.name} [{fps_info} -> stride {effective_stride} @ {fps:.1f}fps]: "
                f"processed {keyframe_num} keyframes in {elapsed_sec:.2f}s "
                f"({ms_per_kf:.1f}ms / keyframe, {kfps:.1f} kfps) -> {len(results)} qualifying detection(s): {summary}"
            )
        else:
            # For clips with no detections: take the first frame as a fast, lightweight JPEG thumbnail
            first_frame = collected_frames[0] if collected_frames else None
            thumb_path = self._save_first_frame_thumbnail(first_frame, clip_path)
            logger.info(
                f"[detector] {clip_path.name} [{fps_info} -> stride {effective_stride} @ {fps:.1f}fps]: "
                f"processed {keyframe_num} keyframes in {elapsed_sec:.2f}s "
                f"({ms_per_kf:.1f}ms / keyframe, {kfps:.1f} kfps) -> 0 qualifying detections"
            )

        if return_frames:
            return results, collected_frames
        return results

    def _save_first_frame_thumbnail(
        self,
        frame: np.ndarray | None,
        clip_path: Path,
    ) -> str | None:
        """
        Saves a single first-frame JPEG snapshot for a zero-detection clip.
        Lightweight (~25 KB) and fast to encode.
        """
        try:
            camera_name = extract_camera_name(
                clip_path,
                incoming_dir=self._incoming_dir,
                custom_mapping=self._settings.get_camera_mapping(),
            )
            date_str = extract_date_str(clip_path)
            target_dir = self._thumbnails_dir / date_str / camera_name
            target_dir.mkdir(parents=True, exist_ok=True)
            dest = target_dir / f"{clip_path.stem}-none.jpg"

            # If frame not in memory, extract directly from video file
            if frame is None:
                cap = cv2.VideoCapture(str(clip_path))
                if cap.isOpened():
                    ret, f = cap.read()
                    cap.release()
                    if ret and f is not None:
                        frame = f

            if frame is None:
                return None

            h, w = frame.shape[:2]
            target_w = 480
            if w > target_w:
                scale = target_w / w
                frame_resized = cv2.resize(frame, (target_w, int(h * scale)), interpolation=cv2.INTER_AREA)
            else:
                frame_resized = frame

            cv2.imwrite(str(dest), frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
            logger.info(f"[detector] saved first-frame JPEG thumbnail for zero-detection clip: {dest}")
            return str(dest)
        except Exception as exc:
            logger.warning(f"[detector] failed to save first-frame thumbnail for {clip_path.name}: {exc}")
            return None

    def _save_snapshot_thumbnail(
        self,
        frame: np.ndarray | None,
        clip_path: Path,
        detections: list[DetectionItem],
        camera_name: str | None = None,
    ) -> str | None:
        """
        Saves an immediate, high-quality single-frame JPEG snapshot of the best detection frame.
        Executes in < 2ms to enable instantaneous push notifications without waiting for GIF encoding.
        """
        try:
            resolved_camera = camera_name or extract_camera_name(
                clip_path,
                incoming_dir=self._incoming_dir,
                custom_mapping=self._settings.get_camera_mapping(),
            )
            date_str = extract_date_str(clip_path)
            suffix_str = format_detection_suffix(detections)
            target_dir = self._thumbnails_dir / date_str / resolved_camera
            target_dir.mkdir(parents=True, exist_ok=True)
            stem = clip_path.stem
            if suffix_str and stem.endswith(suffix_str):
                stem = stem[:-len(suffix_str)]
            dest = target_dir / f"{stem}{suffix_str}.jpg"

            if frame is None:
                cap = cv2.VideoCapture(str(clip_path))
                if cap.isOpened():
                    ret, f = cap.read()
                    cap.release()
                    if ret and f is not None:
                        frame = f

            if frame is None:
                return None

            h, w = frame.shape[:2]
            target_w = 640
            if w > target_w:
                scale = target_w / w
                frame_resized = cv2.resize(frame, (target_w, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
            else:
                frame_resized = frame

            cv2.imwrite(str(dest), frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
            logger.info(f"[detector] saved instant snapshot JPEG thumbnail: {dest}")
            return str(dest)
        except Exception as exc:
            logger.warning(f"[detector] failed to save snapshot thumbnail for {clip_path.name}: {exc}")
            return None

    def render_full_clip_gif(
        self,
        frames: list[np.ndarray],
        clip_path: Path,
        detections: list[DetectionItem],
        camera_name: str | None = None,
        sample_fps: float | None = None,
        max_file_size_bytes: int = 2_500_000,
    ) -> str | None:
        """
        Renders the full-clip animated GIF preview retaining all frames in a dynamic timelapse (~3-5s).
        Designed to run in a background executor thread without blocking the initial notification.
        """
        try:
            if not frames:
                return None

            resolved_camera = camera_name or extract_camera_name(
                clip_path,
                incoming_dir=self._incoming_dir,
                custom_mapping=self._settings.get_camera_mapping(),
            )
            date_str = extract_date_str(clip_path)
            suffix_str = format_detection_suffix(detections)
            target_dir = self._thumbnails_dir / date_str / resolved_camera
            target_dir.mkdir(parents=True, exist_ok=True)
            stem = clip_path.stem
            if suffix_str and stem.endswith(suffix_str):
                stem = stem[:-len(suffix_str)]
            dest = target_dir / f"{stem}{suffix_str}.gif"

            num_frames = len(frames)
            if sample_fps and sample_fps > 0:
                duration_ms = max(50, int(1000.0 / sample_fps))
            else:
                # Dynamic timelapse playback: scale between 150ms and 350ms per frame so whole event plays in ~3-5 seconds
                duration_ms = max(150, min(350, int(4000 / max(1, num_frames))))

            # Width and color depth optimization
            if num_frames <= 12:
                target_w = 480
                num_colors = 128
            elif num_frames <= 25:
                target_w = 400
                num_colors = 96
            elif num_frames <= 40:
                target_w = 340
                num_colors = 64
            else:
                target_w = 280
                num_colors = 48

            for attempt in range(3):
                pil_frames: list[Image.Image] = []
                for f in frames:
                    h, w = f.shape[:2]
                    if w > target_w:
                        scale = target_w / w
                        new_h = max(1, int(h * scale))
                        f_resized = cv2.resize(f, (target_w, new_h), interpolation=cv2.INTER_AREA)
                    else:
                        f_resized = f

                    rgb = cv2.cvtColor(f_resized, cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(rgb).convert(
                        "P", palette=Image.Palette.ADAPTIVE, colors=num_colors
                    )
                    pil_frames.append(pil_img)

                if not pil_frames:
                    return None

                pil_frames[0].save(
                    str(dest),
                    save_all=True,
                    append_images=pil_frames[1:] if len(pil_frames) > 1 else [],
                    duration=duration_ms,
                    loop=0,
                    optimize=True,
                )

                file_size = dest.stat().st_size
                if file_size <= max_file_size_bytes or attempt == 2:
                    logger.info(
                        f"[detector] saved full-clip thumbnail GIF ({len(pil_frames)} frames @ {duration_ms}ms/frame, {target_w}px, {num_colors} colors, {file_size / 1024:.1f} KB): {dest}"
                    )
                    return str(dest)

                target_w = max(200, int(target_w * 0.8))
                num_colors = max(32, int(num_colors * 0.7))

            return str(dest) if dest.is_file() else None
        except Exception as exc:
            logger.warning(f"[detector] thumbnail GIF save failed for {clip_path.name}: {exc}")
            return None

    def _save_animated_thumbnail(
        self,
        frames: list[np.ndarray],
        clip_path: Path,
        detections: list[DetectionItem],
        max_file_size_bytes: int = 2_500_000,
    ) -> str | None:
        """Alias for backward compatibility."""
        return self.render_full_clip_gif(frames, clip_path, detections, max_file_size_bytes=max_file_size_bytes)


