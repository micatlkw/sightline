import asyncio
from pathlib import Path
import pytest
from app.config import Settings
from app.watcher.directory_watcher import DirectoryWatcher


@pytest.mark.asyncio
async def test_watcher_lifecycle(test_settings: Settings):
    queue = asyncio.Queue()
    watcher = DirectoryWatcher(test_settings, queue)
    assert watcher.mode == "polling"

    await watcher.start()
    assert watcher.is_alive is True

    await watcher.stop()
    assert watcher.is_alive is False


@pytest.mark.asyncio
async def test_watcher_wait_for_stable_success(test_settings: Settings, temp_dir: Path):
    queue = asyncio.Queue()
    test_settings.clip_stable_seconds = 1.0
    watcher = DirectoryWatcher(test_settings, queue)

    file_path = temp_dir / "stable_clip.mp4"
    file_path.write_bytes(b"dummy video content 12345")

    is_stable = await watcher.wait_for_stable(file_path)
    assert is_stable is True


@pytest.mark.asyncio
async def test_watcher_wait_for_stable_deleted_file(test_settings: Settings, temp_dir: Path):
    queue = asyncio.Queue()
    watcher = DirectoryWatcher(test_settings, queue)

    file_path = temp_dir / "non_existent.mp4"
    is_stable = await watcher.wait_for_stable(file_path)
    assert is_stable is False
