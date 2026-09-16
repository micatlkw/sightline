import sys
import types
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

# Ensure mocked ultralytics / cv2 / numpy exist if not already installed
if "numpy" not in sys.modules:
    np_mock = types.ModuleType("numpy")
    np_mock.ndarray = type("ndarray", (), {})
    sys.modules["numpy"] = np_mock

if "cv2" not in sys.modules:
    cv2_mock = types.ModuleType("cv2")
    cv2_mock.VideoCapture = type("VideoCapture", (), {})
    cv2_mock.IMWRITE_JPEG_QUALITY = 1
    cv2_mock.imwrite = lambda path, img, params=None: True
    cv2_mock.CAP_PROP_FPS = 5
    cv2_mock.CAP_PROP_FRAME_COUNT = 7
    sys.modules["cv2"] = cv2_mock

if "ultralytics" not in sys.modules:
    ul_mock = types.ModuleType("ultralytics")
    class _MockYOLO:
        names = {0: "person", 2: "car", 3: "motorcycle", 7: "truck", 15: "cat", 16: "dog", 21: "bear"}
        def __init__(self, path):
            self.path = path
        def __call__(self, frame, **kwargs):
            return [types.SimpleNamespace(boxes=[])]
    ul_mock.YOLO = _MockYOLO
    ul_mock.settings = type("SettingsMock", (), {"update": lambda s, d: None})()
    sys.modules["ultralytics"] = ul_mock

from app.config import Settings
from app.database import Database


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    incoming_dir = tmp_path / "incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "test_sightline.db"
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    thumbnails_dir = tmp_path / "thumbnails"
    thumbnails_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        incoming_dir=incoming_dir,
        processed_dir=processed_dir,
        db_path=db_path,
        models_dir=models_dir,
        thumbnails_dir=thumbnails_dir,
        yolo_model="yolo11n.pt",
        target_classes=[0, 2, 3, 7, 15, 16, 21],
        confidence_threshold=0.45,
        vid_stride=30,
        watch_use_polling=True,
        clip_stable_seconds=1.0,
        apprise_urls=[],
        alert_cooldown_seconds=1,
    )


@pytest_asyncio.fixture
async def test_db(test_settings: Settings) -> AsyncIterator[Database]:
    db = Database(test_settings)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
