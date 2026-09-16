from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Setup shims if pydantic / pydantic_settings are not installed in host Python
try:
    import pydantic
    from pydantic import BaseModel, Field, field_validator, model_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
        def model_dump(self):
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def field_validator(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def model_validator(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def Field(default=None, **kwargs):
        return default

    pydantic_mock = MagicMock()
    pydantic_mock.BaseModel = BaseModel
    pydantic_mock.Field = Field
    pydantic_mock.field_validator = field_validator
    pydantic_mock.model_validator = model_validator
    sys.modules["pydantic"] = pydantic_mock

    class BaseSettings(BaseModel):
        pass

    pydantic_settings_mock = MagicMock()
    pydantic_settings_mock.BaseSettings = BaseSettings
    pydantic_settings_mock.SettingsConfigDict = MagicMock
    sys.modules["pydantic_settings"] = pydantic_settings_mock

try:
    import yaml
except ImportError:
    yaml_mock = MagicMock()
    sys.modules["yaml"] = yaml_mock

from app.config import CameraConfig, Settings


class TestCameraSampleFps(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            sample_fps=1.0,
            min_clip_keyframes=5,
            vid_stride=30,
            cameras=[
                CameraConfig(name="Driveway", serial="CAM001", sample_fps=2.0),
                CameraConfig(name="Patio", serial="CAM002", sample_fps=None),
                CameraConfig(name="Backyard", serial="CAM003", sample_fps=0.5),
            ]
        )

    def test_camera_override_resolution(self):
        """Verify per-camera sample_fps override resolution."""
        # Driveway has 2.0 FPS override
        self.assertEqual(self.settings.get_camera_sample_fps("Driveway"), 2.0)
        self.assertEqual(self.settings.get_camera_sample_fps("cam001"), 2.0)

        # Patio has None -> inherits global 1.0 FPS
        self.assertEqual(self.settings.get_camera_sample_fps("Patio"), 1.0)
        self.assertEqual(self.settings.get_camera_sample_fps("CAM002"), 1.0)

        # Backyard has 0.5 FPS override
        self.assertEqual(self.settings.get_camera_sample_fps("Backyard"), 0.5)

        # Unconfigured camera inherits global 1.0 FPS
        self.assertEqual(self.settings.get_camera_sample_fps("UnknownCam"), 1.0)

    def test_camera_framerate_invariance(self):
        """
        Verify that sample_fps translates correctly across different camera frame rates
        to sample exactly the target number of frames per second.
        """
        # 30 fps video with target 1.0 FPS -> vid_stride = 30
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Patio", clip_fps=30.0, total_frames=450)
        self.assertEqual(stride, 30)
        self.assertEqual(eff_fps, 1.0)
        self.assertFalse(adapted)

        # 30 fps video with target 2.0 FPS (Driveway) -> vid_stride = 15
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Driveway", clip_fps=30.0, total_frames=450)
        self.assertEqual(stride, 15)
        self.assertEqual(eff_fps, 2.0)
        self.assertFalse(adapted)

        # 25 fps video with target 1.0 FPS -> vid_stride = 25
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Patio", clip_fps=25.0, total_frames=375)
        self.assertEqual(stride, 25)
        self.assertEqual(eff_fps, 1.0)
        self.assertFalse(adapted)

        # 20 fps video with target 1.0 FPS -> vid_stride = 20
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Patio", clip_fps=20.0, total_frames=300)
        self.assertEqual(stride, 20)
        self.assertEqual(eff_fps, 1.0)
        self.assertFalse(adapted)

        # 15 fps video with target 1.0 FPS -> vid_stride = 15
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Patio", clip_fps=15.0, total_frames=225)
        self.assertEqual(stride, 15)
        self.assertEqual(eff_fps, 1.0)
        self.assertFalse(adapted)

        # 15 fps video with target 0.5 FPS (Backyard) -> vid_stride = 30
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Backyard", clip_fps=15.0, total_frames=150)
        self.assertEqual(stride, 30)
        self.assertEqual(eff_fps, 0.5)
        self.assertFalse(adapted)

    def test_dynamic_minimum_keyframes_short_clip(self):
        """
        Verify that short clips (1-3s) dynamically scale up sampling FPS
        to meet min_clip_keyframes requirement.
        """
        # 2.0s clip @ 30fps (60 frames total), target 1.0 FPS, min_clip_keyframes=5
        # Standard: 2.0s * 1.0 = 2 frames (< 5 frames)
        # Scaled: 5 frames / 2.0s = 2.5 FPS -> vid_stride = round(30 / 2.5) = 12
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Patio", clip_fps=30.0, total_frames=60)
        self.assertTrue(adapted)
        self.assertAlmostEqual(eff_fps, 2.5)
        self.assertEqual(stride, 12)

        # Total keyframes sampled in 60 frames with stride 12 = 60 / 12 = 5 keyframes!
        self.assertEqual(60 // stride, 5)

    def test_dynamic_minimum_keyframes_very_short_clip(self):
        """
        Verify extreme short clip (1.0s @ 15fps), target 1.0 FPS, min_clip_keyframes=5.
        Scaled: 5 / 1.0s = 5.0 FPS -> vid_stride = round(15 / 5.0) = 3
        """
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Patio", clip_fps=15.0, total_frames=15)
        self.assertTrue(adapted)
        self.assertAlmostEqual(eff_fps, 5.0)
        self.assertEqual(stride, 3)
        self.assertEqual(15 // stride, 5)

    def test_normal_clip_does_not_adapt(self):
        """Verify normal 10s clip does not adapt when duration * target_fps >= min_clip_keyframes."""
        # 10.0s @ 30fps = 300 frames, 10.0 * 1.0 = 10 frames >= 5
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Patio", clip_fps=30.0, total_frames=300)
        self.assertFalse(adapted)
        self.assertEqual(eff_fps, 1.0)
        self.assertEqual(stride, 30)

    def test_dynamic_adaptation_disabled_when_min_keyframes_zero(self):
        """Verify dynamic adaptation is bypassed when min_clip_keyframes is 0 or 1."""
        cfg = Settings(sample_fps=1.0, min_clip_keyframes=0)
        stride, eff_fps, adapted = cfg.resolve_clip_stride_and_fps("AnyCam", clip_fps=30.0, total_frames=60)
        self.assertFalse(adapted)
        self.assertEqual(eff_fps, 1.0)
        self.assertEqual(stride, 30)

    def test_zero_or_corrupted_metadata_fallback(self):
        """Verify that zero frames or zero fps handles gracefully without division by zero."""
        stride, eff_fps, adapted = self.settings.resolve_clip_stride_and_fps("Patio", clip_fps=0.0, total_frames=0)
        self.assertFalse(adapted)
        self.assertEqual(eff_fps, 1.0)
        self.assertEqual(stride, 30)

    def test_legacy_vid_stride_migration(self):
        """Verify that legacy vid_stride: 15 converts to sample_fps: 2.0."""
        # Test class method migration
        migrated = Settings._migrate_sample_fps_and_stride({"vid_stride": 15})
        self.assertEqual(migrated.get("sample_fps"), 2.0)

        migrated_30 = Settings._migrate_sample_fps_and_stride({"vid_stride": 30})
        self.assertEqual(migrated_30.get("sample_fps"), 1.0)

        # Test reverse migration: sample_fps: 0.5 -> vid_stride: 60
        migrated_reverse = Settings._migrate_sample_fps_and_stride({"sample_fps": 0.5})
        self.assertEqual(migrated_reverse.get("vid_stride"), 60)

    def test_export_and_yaml_formatting(self):
        """Verify export_editable_dict and render_yaml_template contain sample_fps and min_clip_keyframes."""
        d = self.settings.export_editable_dict()
        self.assertIn("sample_fps", d)
        self.assertIn("min_clip_keyframes", d)
        self.assertEqual(d["sample_fps"], 1.0)
        self.assertEqual(d["min_clip_keyframes"], 5)

        yaml_text = self.settings.render_yaml_template()
        self.assertIn("sample_fps: 1.0", yaml_text)
        self.assertIn("min_clip_keyframes: 5", yaml_text)
        self.assertIn("sample_fps: 2.0", yaml_text)  # For Driveway

    def test_update_with_synchronization(self):
        """Verify update_with keeps sample_fps and vid_stride synchronized."""
        # 1. Updating sample_fps updates vid_stride
        updated = self.settings.update_with({"sample_fps": 2.0})
        self.assertEqual(updated.sample_fps, 2.0)
        self.assertEqual(updated.vid_stride, 15)

        # 2. Updating legacy vid_stride updates sample_fps
        updated2 = self.settings.update_with({"vid_stride": 10})
        self.assertEqual(updated2.vid_stride, 10)
        self.assertEqual(updated2.sample_fps, 3.0)

        # 3. Updating camera sample_fps
        updated3 = self.settings.update_with({
            "cameras": [
                {"name": "Driveway", "serial": "CAM001", "sample_fps": 3.0, "enabled": True}
            ]
        })
        self.assertEqual(updated3.get_camera_sample_fps("Driveway"), 3.0)

    def test_confidence_threshold_percentage_normalization(self):
        """Verify that passing confidence threshold as percentage (1-100) or decimal (0-1) works correctly."""
        # 1. CameraConfig validator
        self.assertEqual(CameraConfig._validate_confidence_threshold(60), 0.60)
        self.assertEqual(CameraConfig._validate_confidence_threshold(0.60), 0.60)
        self.assertIsNone(CameraConfig._validate_confidence_threshold(None))

        # 2. Settings validator
        self.assertEqual(Settings._validate_global_confidence_threshold(45), 0.45)
        self.assertEqual(Settings._validate_global_confidence_threshold(0.45), 0.45)


if __name__ == "__main__":
    unittest.main()
