from pathlib import Path
from app.detector.yolo_detector import YoloDetector


def test_resolve_model_already_in_models_dir(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    model_file = models_dir / "yolo11s.pt"
    model_file.write_bytes(b"dummy model weights content " * 100)

    resolved = YoloDetector._resolve_or_download_model(models_dir, "yolo11s.pt")
    assert resolved == model_file
    assert resolved.exists()


def test_resolve_model_moved_from_cwd(tmp_path: Path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    cwd_dir = tmp_path / "app"
    cwd_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd_dir)

    cwd_model = cwd_dir / "yolo11s.pt"
    cwd_model.write_bytes(b"dummy weights from cwd " * 100)

    resolved = YoloDetector._resolve_or_download_model(models_dir, "yolo11s.pt")
    assert resolved == models_dir / "yolo11s.pt"
    assert resolved.exists()
    assert not cwd_model.exists()  # should have been moved into models_dir


def test_save_animated_thumbnail_with_detections(tmp_path: Path):
    import numpy as np
    from PIL import Image
    from app.config import Settings
    from app.models.event import DetectionItem

    settings = Settings(
        incoming_dir=tmp_path / "incoming",
        processed_dir=tmp_path / "processed",
        thumbnails_dir=tmp_path / "thumbnails",
    )
    detector = YoloDetector.__new__(YoloDetector)
    detector._thumbnails_dir = settings.thumbnails_dir
    detector._incoming_dir = settings.incoming_dir
    detector._cameras_config_path = settings.cameras_config_path

    clip_path = tmp_path / "incoming" / "FrontDoor_0001_20260830_120000.mp4"
    frames = [
        np.zeros((480, 640, 3), dtype=np.uint8),
        np.ones((480, 640, 3), dtype=np.uint8) * 255,
    ]
    detections = [
        DetectionItem(class_id=0, class_name="person", confidence=0.9, timestamp_sec=1.0, bbox=[0, 0, 1, 1])
    ]

    saved_path = detector._save_animated_thumbnail(frames, clip_path, detections)
    assert saved_path is not None
    gif_file = Path(saved_path)
    assert gif_file.exists()
    assert gif_file.parent == tmp_path / "thumbnails" / "2026-08-30" / "FrontDoor"
    assert gif_file.name == "FrontDoor_0001_20260830_120000-1person.gif"

    with Image.open(gif_file) as img:
        assert getattr(img, "is_animated", False) is True
        assert img.n_frames == 2


def test_save_animated_thumbnail_no_detections(tmp_path: Path):
    import numpy as np
    from PIL import Image
    from app.config import Settings

    settings = Settings(
        incoming_dir=tmp_path / "incoming",
        processed_dir=tmp_path / "processed",
        thumbnails_dir=tmp_path / "thumbnails",
    )
    detector = YoloDetector.__new__(YoloDetector)
    detector._thumbnails_dir = settings.thumbnails_dir
    detector._incoming_dir = settings.incoming_dir
    detector._cameras_config_path = settings.cameras_config_path

    clip_path = tmp_path / "incoming" / "Driveway_0002_20260830_130000.mp4"
    frames = [
        np.zeros((720, 1280, 3), dtype=np.uint8),
        np.ones((720, 1280, 3), dtype=np.uint8) * 128,
        np.ones((720, 1280, 3), dtype=np.uint8) * 255,
    ]

    saved_path = detector._save_animated_thumbnail(frames, clip_path, [])
    assert saved_path is not None
    gif_file = Path(saved_path)
    assert gif_file.exists()
    assert gif_file.parent == tmp_path / "thumbnails" / "2026-08-30" / "Driveway"
    assert gif_file.name == "Driveway_0002_20260830_130000-none.gif"

    with Image.open(gif_file) as img:
        assert getattr(img, "is_animated", False) is True
        assert img.n_frames == 3
        # Ensure resized to target width (480px for <= 12 frames)
        assert img.size[0] == 480


def test_save_animated_thumbnail_high_frame_count_size_safeguard(tmp_path: Path):
    import numpy as np
    from PIL import Image
    from app.config import Settings
    from app.models.event import DetectionItem

    settings = Settings(
        incoming_dir=tmp_path / "incoming",
        processed_dir=tmp_path / "processed",
        thumbnails_dir=tmp_path / "thumbnails",
    )
    detector = YoloDetector.__new__(YoloDetector)
    detector._thumbnails_dir = settings.thumbnails_dir
    detector._incoming_dir = settings.incoming_dir
    detector._cameras_config_path = settings.cameras_config_path

    clip_path = tmp_path / "incoming" / "Backyard_0003_20260830_140000.mp4"
    # Generate 50 frames with noise to simulate complex video
    np.random.seed(42)
    frames = [np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8) for _ in range(50)]
    detections = [
        DetectionItem(class_id=0, class_name="person", confidence=0.95, timestamp_sec=1.0, bbox=[0, 0, 1, 1])
    ]

    saved_path = detector._save_animated_thumbnail(frames, clip_path, detections, max_file_size_bytes=500_000)
    assert saved_path is not None
    gif_file = Path(saved_path)
    assert gif_file.exists()
    assert gif_file.stat().st_size <= 500_000

    with Image.open(gif_file) as img:
        assert getattr(img, "is_animated", False) is True
        assert img.n_frames == 50


def test_detect_sync_per_class_confidence_filtering(tmp_path: Path):
    import numpy as np
    from app.config import Settings

    settings = Settings(
        incoming_dir=tmp_path / "incoming",
        processed_dir=tmp_path / "processed",
        thumbnails_dir=tmp_path / "thumbnails",
    )
    detector = YoloDetector.__new__(YoloDetector)
    detector._settings = settings
    detector._conf_threshold = 0.45
    detector._vid_stride = 30
    detector._target_classes = [0, 2, 16]
    detector._thumbnails_dir = settings.thumbnails_dir
    detector._incoming_dir = settings.incoming_dir
    detector._cameras_config_path = settings.cameras_config_path

    class MockBox:
        def __init__(self, cls_id, conf, xyxyn):
            self.cls = [cls_id]
            self.conf = [conf]
            self.xyxyn = [type("Tensor", (), {"tolist": lambda self: xyxyn})()]

    img = np.zeros((480, 640, 3), dtype=np.uint8)

    class MockResult:
        def __init__(self, boxes, orig_img):
            self.boxes = boxes
            self.orig_img = orig_img
        def plot(self):
            return self.orig_img
        def __getitem__(self, indices):
            return MockResult([self.boxes[i] for i in indices], self.orig_img)

    boxes = [
        MockBox(cls_id=0, conf=0.58, xyxyn=[0.1, 0.1, 0.5, 0.5]),    # person: 58%
        MockBox(cls_id=16, conf=0.54, xyxyn=[0.2, 0.2, 0.6, 0.6]),   # dog: 54%
        MockBox(cls_id=2, conf=0.48, xyxyn=[0.3, 0.3, 0.7, 0.7]),    # car: 48%
    ]

    class MockModel:
        def __init__(self):
            self.names = {0: "person", 2: "car", 16: "dog"}
        def __call__(self, *args, **kwargs):
            return [MockResult(boxes, img)]

    detector._model = MockModel()

    clip_file = tmp_path / "incoming" / "clip.mp4"
    clip_file.parent.mkdir(parents=True, exist_ok=True)
    clip_file.write_bytes(b"dummy")

    # Person threshold = 60%, Dog threshold = 50%, fallback = 45%
    # Person (58% < 60%) should be rejected
    # Dog (54% >= 50%) should be accepted
    # Car (48% >= fallback 45%) should be accepted
    results = detector.detect_sync(
        clip_path=clip_file,
        target_classes=[0, 2, 16],
        conf_threshold=0.45,
        camera_name="frontdoor",
        class_conf_thresholds={0: 0.60, 16: 0.50},
    )

    assert len(results) == 2
    classes = [d.class_name for d in results]
    assert "person" not in classes
    assert "dog" in classes
    assert "car" in classes




