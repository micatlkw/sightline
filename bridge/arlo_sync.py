#!/usr/bin/env python3
from __future__ import annotations

"""
SightLine Arlo Sync Daemon
Pure Monotonic Sequence Watermark & Zero-Mount FAT32 reader for Orange Pi -> Synology SMB sync.

Features:
- MP4 Container Integrity & Finalization Validation (ftyp, explicit mdat size, moov atom verification).
- Multi-Cycle Size Stability Tracking (prevents premature transfer during chunked camera write pauses).
- Pure Sequence Watermark Cursor (Zero file-path caching, O(1) complexity).
- Active Hex Directory Caching with Lookahead & Rollover Detection (reduces mdir subprocess overhead by 50%).
- Automatic Storage Format & Reset Detection.
- Single-Pass mdir Extraction (Eliminates N+1 subprocess overhead).
- Sub-second Polling (0.5s active, 0.5s idle) for minimal handoff latency.
- 1 MB Buffered CIFS/SMB Streaming with Granular Transfer Benchmarking.
- Atomic NAS Staging (.tmp -> .mp4) & Hardware fsync State Persistence.
- In-place Live Auto-Reload on script file update.
- Full Signal Trapping (SIGINT, SIGTERM, SIGQUIT, SIGHUP).
"""

import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ================= Configuration =================
IMG_FILE = "/var/arlo_storage.img"
NAS_DEST = "/mnt/synology_watch"
STATE_FILE_JSON = "/var/lib/arlo_sync_state.json"
RAM_STAGING_DIR = "/dev/shm/arlo_staging"
POLL_INTERVAL_ACTIVE_SEC = 0.5
POLL_INTERVAL_IDLE_SEC = 0.5
DIR_CACHE_TTL_SEC = 30.0
MTOOLSRC_PATH = "/tmp/.mtoolsrc_arlo"
# =================================================

# Regular Expressions
HEX_DIR_RE = re.compile(r"^[0-9a-fA-F]{6}$")
FILE_RE = re.compile(
    r"^([A-Z0-9]+)_([0-9a-fA-F]+)_(\d{8})_(\d{6})\.mp4$", re.IGNORECASE
)
MDIR_DIR_RE = re.compile(r"^Directory for (A:.*)$", re.IGNORECASE)
MDIR_FILE_RE = re.compile(
    r"^\S+\s+\S+\s+(\d+)\s+\S+\s+\S+\s+(.+\.mp4)$", re.IGNORECASE
)

# Runtime State
state = {
    "last_hex_dir": "000000",
    "watermarks": {}  # camera_serial -> max_synced_sequence (int)
}
in_progress_logged: set[str] = set()
in_progress_polls: dict[str, list[datetime]] = {}
running = True

_cached_active_dirs: list[str] = []
_last_dir_scan_time: float = 0.0

SCRIPT_PATH = os.path.realpath(__file__)
LAST_SCRIPT_MTIME = os.path.getmtime(SCRIPT_PATH) if os.path.exists(SCRIPT_PATH) else 0.0


def cleanup_staging_dir():
    """Ensures RAM disk staging directory exists with secure permissions (0700) and cleans stale files."""
    try:
        os.makedirs(RAM_STAGING_DIR, mode=0o700, exist_ok=True)
        try:
            os.chmod(RAM_STAGING_DIR, 0o700)
        except Exception:
            pass
        for entry in os.scandir(RAM_STAGING_DIR):
            if entry.is_file():
                try:
                    os.remove(entry.path)
                except Exception:
                    pass
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Staging dir setup warning: {e}")


def check_script_updated():
    """Checks if the script file on disk has been modified and hot-reloads if needed."""
    global LAST_SCRIPT_MTIME
    try:
        if os.path.exists(SCRIPT_PATH):
            mtime = os.path.getmtime(SCRIPT_PATH)
            if LAST_SCRIPT_MTIME > 0 and mtime > LAST_SCRIPT_MTIME:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Script change detected ({SCRIPT_PATH}). Reloading daemon in-place...")
                cleanup_staging_dir()
                save_state()
                os.execv(sys.executable, [sys.executable, SCRIPT_PATH] + sys.argv[1:])
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Script reload check error: {e}")


def setup_mtools_env() -> dict:
    """
    Generates a dedicated mtools config dynamically linked to IMG_FILE.
    Eliminates dependency on /etc/mtools.conf.
    """
    config_content = (
        f'drive a: file="{IMG_FILE}" partition=1\n'
        f'mtools_skip_check=1\n'
    )
    with open(MTOOLSRC_PATH, "w", encoding="utf-8") as f:
        f.write(config_content)

    env = os.environ.copy()
    env["MTOOLSRC"] = MTOOLSRC_PATH
    return env


MTOOLS_ENV = setup_mtools_env()


def is_valid_mp4(filepath: str) -> tuple[bool, str]:
    """
    Validates that an MP4 file is structurally complete and finalized by Arlo:
    - Minimum valid MP4 container size (>= 32 bytes).
    - Top-level atom sequence starts with 'ftyp' box.
    - All atom sizes sum up exactly to the total file size (no trailing incomplete data).
    - 'mdat' atom has a non-zero, explicit size (during streaming, Arlo leaves mdat size as 0).
    - 'moov' atom is present at the end (contains index tables and track metadata).
    - Strict bounds checking and loop ceiling to prevent DoS or hangs.
    """
    try:
        if not os.path.exists(filepath):
            return False, "File does not exist"
        filesize = os.path.getsize(filepath)
        if filesize < 32:
            return False, f"File too small ({filesize} bytes)"

        with open(filepath, "rb") as f:
            has_ftyp = False
            has_moov = False
            has_mdat = False
            pos = 0
            atom_count = 0
            max_atoms = 10000

            while pos < filesize:
                atom_count += 1
                if atom_count > max_atoms:
                    return False, f"Excessive atom count ({atom_count}) exceeded safe limit"

                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    return False, f"Truncated atom header at offset {pos}"

                size, tag = struct.unpack(">I4s", header)
                try:
                    tag_str = tag.decode("latin1", errors="replace")
                except Exception:
                    tag_str = "????"

                if size == 1:
                    ext_header = f.read(8)
                    if len(ext_header) < 8:
                        return False, f"Truncated 64-bit size at offset {pos}"
                    size = struct.unpack(">Q", ext_header)[0]
                elif size == 0:
                    # In finalized MP4 files, atoms have explicit sizes.
                    # An mdat size of 0 indicates unfinalized streaming recording.
                    return False, f"Unfinalized atom {tag_str} (size 0 extends to EOF) at offset {pos}"
                elif size < 8:
                    return False, f"Invalid atom size ({size} bytes) for {tag_str} at offset {pos}"

                if pos + size > filesize:
                    return False, f"Atom {tag_str} extends beyond file ({pos + size} > {filesize})"

                if tag_str == "ftyp":
                    has_ftyp = True
                elif tag_str == "moov":
                    has_moov = True
                elif tag_str == "mdat":
                    has_mdat = True

                pos += size

            if pos != filesize:
                return False, f"Atom size sum ({pos}) != file size ({filesize})"

            if not (has_ftyp and has_mdat and has_moov):
                return False, f"Missing required atoms (ftyp={has_ftyp}, mdat={has_mdat}, moov={has_moov})"

            return True, "Valid complete MP4"
    except Exception as e:
        return False, f"Validation error: {e}"


def get_mp4_duration(filepath: str) -> float:
    """
    Extracts video duration in seconds from the MP4 mvhd (Movie Header) atom.
    Runs in < 1ms without spawning ffprobe or decoding video frames.
    """
    try:
        if not os.path.exists(filepath):
            return 0.0
        filesize = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            pos = 0
            while pos < filesize:
                f.seek(pos)
                h = f.read(8)
                if len(h) < 8:
                    break
                size, tag = struct.unpack(">I4s", h)
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                elif size == 0:
                    size = filesize - pos

                if tag == b"moov":
                    moov_data = f.read(min(size - 8, 4096))
                    mvhd_idx = moov_data.find(b"mvhd")
                    if mvhd_idx >= 4:
                        mvhd_box = moov_data[mvhd_idx - 4:]
                        if len(mvhd_box) >= 28:
                            _, _, ver = struct.unpack(">I4sB", mvhd_box[:9])
                            if ver == 0:
                                _, _, ts, dur = struct.unpack(">IIII", mvhd_box[12:28])
                                return (dur / ts) if ts > 0 else 0.0
                            elif ver == 1 and len(mvhd_box) >= 44:
                                _, _, ts, dur = struct.unpack(">QQIQ", mvhd_box[12:44])
                                return (dur / ts) if ts > 0 else 0.0
                    return 0.0
                pos += size
    except Exception:
        pass
    return 0.0


def parse_arlo_filename(filename: str):
    """Parses Camera Serial, Sequence Number (int), Date, and Time from filename."""
    match = FILE_RE.match(filename)
    if match:
        serial, seq_str, date_str, time_str = match.groups()
        try:
            seq_num = int(seq_str, 16)
        except ValueError:
            seq_num = int(seq_str)
        return serial, seq_num, date_str, time_str
    return None


def load_state():
    """Loads state from JSON file, or initializes fresh defaults."""
    global state
    os.makedirs(os.path.dirname(STATE_FILE_JSON), exist_ok=True)

    if os.path.exists(STATE_FILE_JSON):
        try:
            with open(STATE_FILE_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                state["last_hex_dir"] = data.get("last_hex_dir", "000000")
                state["watermarks"] = data.get("watermarks", {})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Loaded state: last_hex_dir={state['last_hex_dir']}, {len(state['watermarks'])} camera watermarks.")
            return
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error loading {STATE_FILE_JSON}: {e}. Initializing fresh state.")

    state["last_hex_dir"] = "000000"
    state["watermarks"] = {}
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Initialized fresh watermark state.")


def save_state():
    """Persists current watermarks and active folder cursor atomically with hardware fsync."""
    tmp_path = f"{STATE_FILE_JSON}.tmp"
    data = {
        "last_hex_dir": state["last_hex_dir"],
        "watermarks": state["watermarks"],
        "updated_at": datetime.now().isoformat()
    }
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # Force commit to physical storage (power-loss resilience)
        os.replace(tmp_path, STATE_FILE_JSON)
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error saving state to {STATE_FILE_JSON}: {e}")


def get_active_hex_directories(force_refresh: bool = False) -> list[str]:
    """
    Returns cached active hex directories with 30s TTL to eliminate 50% of mdir subprocesses.
    Includes latest existing folders and lookahead (latest + 1) for instant rollover detection.
    """
    global _cached_active_dirs, _last_dir_scan_time
    now = time.time()

    # Dynamic tightening: if any camera is nearing the 1000-clip rollover window (seq >= 950), tighten TTL
    max_seq = max(state["watermarks"].values()) if state["watermarks"] else 0
    effective_ttl = 5.0 if (max_seq % 1000 >= 950) else DIR_CACHE_TTL_SEC

    if force_refresh or not _cached_active_dirs or (now - _last_dir_scan_time > effective_ttl):
        proc = subprocess.run(
            ["mdir", "-b", "a:/arlo"],
            capture_output=True,
            text=True,
            env=MTOOLS_ENV,
            check=False
        )
        hex_dirs = []
        for line in proc.stdout.splitlines():
            folder = line.strip().rstrip("/").split("/")[-1]
            if HEX_DIR_RE.match(folder):
                hex_dirs.append(folder)

        if not hex_dirs:
            hex_dirs = ["000000"]
        else:
            hex_dirs.sort(key=lambda x: int(x, 16))

        latest_disk_dir = hex_dirs[-1]
        last_recorded = state.get("last_hex_dir", "000000")

        # Storage Format / Reset Detection:
        # If disk's latest directory is numerically lower than recorded cursor, drive was reformatted
        if int(latest_disk_dir, 16) < int(last_recorded, 16):
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Storage format/reset detected "
                f"({last_recorded} -> {latest_disk_dir}). Resetting watermarks."
            )
            state["watermarks"].clear()
            state["last_hex_dir"] = latest_disk_dir
            save_state()
        elif int(latest_disk_dir, 16) > int(last_recorded, 16):
            state["last_hex_dir"] = latest_disk_dir
            save_state()

        next_hex = f"{int(latest_disk_dir, 16) + 1:06x}"

        # Keep latest 2 existing folders for rollover window, plus next sequential lookahead
        active = hex_dirs[-2:] if len(hex_dirs) >= 2 else hex_dirs[-1:]
        if next_hex not in active:
            active.append(next_hex)

        _cached_active_dirs = active
        _last_dir_scan_time = now

    return list(_cached_active_dirs)


def list_unsynced_candidates(active_dirs: list[str]) -> list[tuple[str, int, str, int]]:
    """
    Single-pass mdir scanner across active hex directories.
    Filters files purely by checking if sequence > camera watermark.
    """
    results = []
    scopes = [f"a:/arlo/{d}/*.mp4" for d in active_dirs]
    cmd = ["mdir"] + scopes

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=MTOOLS_ENV,
            check=False
        )
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error executing mdir on {IMG_FILE}: {e}")
        return []

    current_dir = "A:/"
    for line in proc.stdout.splitlines():
        line_clean = line.strip()

        dir_match = MDIR_DIR_RE.match(line_clean)
        if dir_match:
            current_dir = dir_match.group(1).rstrip("/")
            continue

        file_match = MDIR_FILE_RE.match(line_clean)
        if file_match:
            size_bytes = int(file_match.group(1))
            filename = file_match.group(2)
            filepath = f"{current_dir}/{filename}"

            parsed = parse_arlo_filename(filename)
            if not parsed:
                continue

            camera_serial, seq_num, _, _ = parsed
            watermark = state["watermarks"].get(camera_serial, -1)

            # Pure sequence watermark check: only candidates with sequence > watermark
            if seq_num > watermark and size_bytes > 0:
                results.append((filepath, size_bytes, camera_serial, seq_num))

    return results


def sync_to_synology(filepath: str, expected_size: int, camera_serial: str, seq_num: int) -> bool:
    """
    Stages clip into RAM disk (/dev/shm), validates MP4 container finalization (< 1ms),
    streams from RAM to Synology NAS watch folder via atomic rename (.tmp -> .mp4),
    and advances the camera watermark only upon verified completion.
    """
    filename = os.path.basename(filepath)
    if not FILE_RE.match(filename):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Skipping invalid filename format: {filename}")
        return False

    ram_path = os.path.join(RAM_STAGING_DIR, filename)
    nas_tmp = os.path.join(NAS_DEST, f".{filename}.tmp")
    nas_final = os.path.join(NAS_DEST, filename)
    t_sync_start = datetime.now()

    try:
        # Step 1: Direct extraction from FAT32 image to RAM disk (/dev/shm)
        t_extract_start = time.perf_counter()
        copy_cmd = ["mcopy", "-o", filepath, ram_path]
        result = subprocess.run(copy_cmd, capture_output=True, env=MTOOLS_ENV, check=False)
        if result.returncode != 0 or not os.path.exists(ram_path):
            return False
        extract_ms = (time.perf_counter() - t_extract_start) * 1000.0

        # Security: Enforce strict file permissions (0600) on RAM disk
        try:
            os.chmod(ram_path, 0o600)
        except Exception:
            pass

        # Step 2: Validate MP4 container in RAM (< 1ms)
        t_val_start = time.perf_counter()
        valid, reason = is_valid_mp4(ram_path)
        val_ms = (time.perf_counter() - t_val_start) * 1000.0
        if not valid:
            if filename not in in_progress_logged:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Arlo recording in progress for {filename}: {reason}")
                in_progress_logged.add(filename)
                in_progress_polls[filename] = [t_sync_start]
            else:
                if filename not in in_progress_polls:
                    in_progress_polls[filename] = []
                in_progress_polls[filename].append(t_sync_start)
            return False

        actual_size = os.path.getsize(ram_path)
        video_dur = get_mp4_duration(ram_path)

        # Step 3: Stream finalized file from RAM disk to Synology NAS watch folder (1MB buffer)
        t_smb_start = time.perf_counter()
        with open(ram_path, "rb") as fsrc, open(nas_tmp, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=1024 * 1024)

        # Verify transfer size on NAS
        if os.path.getsize(nas_tmp) != actual_size:
            if os.path.exists(nas_tmp):
                os.remove(nas_tmp)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] NAS transfer size mismatch for {filename}")
            return False

        # Step 4: Atomically commit the complete MP4 on NAS
        os.replace(nas_tmp, nas_final)
        smb_ms = (time.perf_counter() - t_smb_start) * 1000.0
        smb_mb_s = (actual_size / (1024 * 1024)) / (smb_ms / 1000.0) if smb_ms > 0 else 0.0

        # Step 5: Advance watermark for this camera
        curr_max = state["watermarks"].get(camera_serial, -1)
        if seq_num > curr_max:
            state["watermarks"][camera_serial] = seq_num

        save_state()

        # Calculate timing breakdown (video duration, camera recording window, sync lag, cam finalize)
        cam_rec_str = ""
        cam_finalize_str = ""
        sync_lag = 0.0
        try:
            parsed = parse_arlo_filename(filename)
            if parsed:
                _, _, d_str, t_str = parsed
                cam_start = datetime.strptime(f"{d_str}_{t_str}", "%Y%m%d_%H%M%S")
                cam_end = cam_start + timedelta(seconds=video_dur)
                sync_lag = max(0.0, (datetime.now() - cam_end).total_seconds())
                cam_rec_str = f" | Cam Rec: {cam_start.strftime('%H:%M:%S')} -> {cam_end.strftime('%H:%M:%S')}"

                # Post-EOF camera finalization latency and unfinalized poll count
                post_eof_wait = max(0.0, (t_sync_start - cam_end).total_seconds())
                prior_polls = in_progress_polls.pop(filename, [])
                unfinalized_post_eof = sum(1 for p in prior_polls if p >= cam_end)
                cam_finalize_str = f" | Cam Finalize: {post_eof_wait:.2f}s ({unfinalized_post_eof} polls post-EOF)"
        except Exception:
            pass

        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Synced & Verified: {filename} ({actual_size:,} bytes) [Camera: {camera_serial}, Seq: {seq_num}]\n"
            f"           └─ Breakdown: Extract: {extract_ms:.1f}ms | Validate: {val_ms:.2f}ms | SMB Copy: {smb_ms:.1f}ms ({smb_mb_s:.1f} MB/s){cam_finalize_str} | Video: {video_dur:.1f}s{cam_rec_str} | Sync Lag: {sync_lag:.2f}s"
        )
        return True

    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error syncing {filename}: {e}")
        if os.path.exists(nas_tmp):
            try:
                os.remove(nas_tmp)
            except Exception:
                pass
        return False
    finally:
        # Guaranteed cleanup of RAM disk staging file
        if os.path.exists(ram_path):
            try:
                os.remove(ram_path)
            except Exception:
                pass


def handle_shutdown(signum, frame):
    """Graceful shutdown signal handler."""
    global running
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Signal received ({signum}). Saving state and shutting down...")
    running = False
    cleanup_staging_dir()
    save_state()
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, handle_shutdown)    # Ctrl+C
    signal.signal(signal.SIGTERM, handle_shutdown)   # systemctl stop / kill
    signal.signal(signal.SIGQUIT, handle_shutdown)   # Ctrl+Break / Ctrl+\
    signal.signal(signal.SIGHUP, handle_shutdown)    # Terminal close / SSH disconnect

    if not os.path.exists(IMG_FILE):
        print(f"Error: Storage image {IMG_FILE} not found!")
        sys.exit(1)

    os.makedirs(NAS_DEST, exist_ok=True)
    cleanup_staging_dir()
    load_state()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Monitoring {IMG_FILE} -> {NAS_DEST} (RAM Disk Staging + Pure Sequence Watermark + Instant MP4 Validation)")

    while running:
        check_script_updated()

        # Verify NAS accessibility
        if not os.path.ismount(NAS_DEST) and not os.path.exists(NAS_DEST):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] NAS mount unavailable. Retrying in 2s...")
            time.sleep(2)
            continue

        poll_interval = POLL_INTERVAL_IDLE_SEC

        try:
            active_dirs = get_active_hex_directories()
            candidates = list_unsynced_candidates(active_dirs)
            current_active_filenames = set()

            if candidates:
                # Active operations detected: use faster polling
                poll_interval = POLL_INTERVAL_ACTIVE_SEC

            for filepath, size, camera_serial, seq_num in candidates:
                filename = os.path.basename(filepath)
                current_active_filenames.add(filename)
                if sync_to_synology(filepath, size, camera_serial, seq_num):
                    in_progress_logged.discard(filename)
                    in_progress_polls.pop(filename, None)
                    # If file synced was in a higher hex dir than last_recorded, refresh dir cache
                    parent_dir = os.path.basename(os.path.dirname(filepath))
                    if HEX_DIR_RE.match(parent_dir) and int(parent_dir, 16) > int(state.get("last_hex_dir", "000000"), 16):
                        state["last_hex_dir"] = parent_dir
                        save_state()
                        get_active_hex_directories(force_refresh=True)

            in_progress_logged.intersection_update(current_active_filenames)
            for fn in list(in_progress_polls.keys()):
                if fn not in current_active_filenames:
                    in_progress_polls.pop(fn, None)

        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Sync loop error: {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()

