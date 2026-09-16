# sightline-bridge: Arlo USB Storage Bridge & Sync Daemon

Hardware USB Mass Storage emulation and zero-mount FAT32 synchronization daemon for Linux single-board computers (Orange Pi, Raspberry Pi, and compatible SBCs).

This subsystem bridges local video recordings from proprietary **Arlo SmartHubs and Base Stations** (e.g., VMB4000, VMB4500, VMB4540, VMB5000) directly into the [**Sightline Core**](../core) surveillance pipeline on your Synology NAS or Linux server in real time.

---

## Architecture & System Overview

Arlo Base Stations only store video clips locally to an attached physical USB storage drive formatted with FAT32. There is no official local API, RTSP stream, or webhook provided by Arlo to extract clips in real time.

The **Sightline Bridge** solves this by emulating a high-speed USB flash drive using the Linux USB Gadget subsystem (`libcomposite` + `configfs`). The Arlo Base Station detects the bridge SBC as a standard USB flash drive and writes `.mp4` video clips directly to a virtual disk image (`/var/arlo_storage.img`). Simultaneously, the `arlo_sync.py` daemon monitors this image and streams completed recordings over CIFS/SMB to the Synology NAS watch folder with sub-second handoff latency.

```
┌─────────────────────────────────────────────────────────────┐
│ Arlo Wire-Free Cameras                                      │
└──────────────────────────────┬──────────────────────────────┘
                               │ Wi-Fi Recording Stream
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Arlo Base Station / SmartHub (USB Host)                     │
│ (VMB4000 / VMB4500 / VMB4540 / VMB5000)                     │
└──────────────────────────────┬──────────────────────────────┘
                               │ USB OTG Cable (Mass Storage Device)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Bridge SBC (Orange Pi / Raspberry Pi / Linux SBC)           │
│                                                             │
│   /var/arlo_storage.img (Virtual FAT32 Disk Image)          │
│          │                                                  │
│          │ (Zero-Mount Userland Read via mtools / mdir)     │
│          ▼                                                  │
│   arlo_sync.py (Sync Daemon)                                │
│     ├── 0.5s Single-Pass mdir Scanner                       │
│     ├── Pure Monotonic Sequence Watermarking                │
│     ├── Extraction to RAM Disk (/dev/shm/arlo_staging)      │
│     ├── Instant In-Memory MP4 Validation (<1ms)             │
│     │   (Checks ftyp, explicit mdat size, moov box)         │
│     └── 1 MB Buffered CIFS/SMB Stream                       │
└──────────────────────────────┬──────────────────────────────┘
                               │ SMB/CIFS Network Transfer (.tmp -> .mp4)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ Synology NAS / Linux Server (/mnt/synology_watch)           │
│                                                             │
│   sightline-core (Docker)                                   │
│     ├── inotify Watcher (Instant Finalized Detection)       │
│     ├── CPU-Optimized YOLO Object Detection                 │
│     └── Push Notifications (Web Push / FCM / Apprise)       │
└─────────────────────────────────────────────────────────────┘
```

---

## The Dual-Host Concurrency Solution: Zero-Mount FAT32

Connecting two computers to the same physical or virtual storage simultaneously typically causes catastrophic filesystem corruption. FAT32 has no multi-host clustering, locking, or distributed cache coherence mechanisms:

1. **The Problem**: If the bridge SBC kernel mounts `/var/arlo_storage.img` via `mount -t vfat`, the Linux VFS and page cache buffer file metadata and sector allocations in memory. When the Arlo hub writes new clusters via USB, the host kernel remains unaware of sector changes, leading to stale directory views, split-brain corruption, and filesystem destruction.
2. **The Solution**: **Zero-Mount Access via `mtools`**. The bridge SBC **never** mounts `/var/arlo_storage.img` into the operating system directory tree. Instead, userland tools (`mdir` and `mcopy` from the `mtools` suite) read the raw FAT32 sectors directly on demand. This allows read-only inspection and extraction of files while the Arlo Base Station writes to the virtual drive concurrently without any risk of filesystem corruption.

---

## Key Features

- **Sub-Second Handoff Latency**: Operates with 0.5-second polling intervals (active and idle) to pass recordings to `sightline-core` within seconds of camera trigger completion.
- **In-Memory MP4 Container Validation**: Arlo streams video by writing an open `mdat` atom (size `0`, indicating streaming to EOF). Only when recording stops does Arlo write the final atom size and the `moov` index atom. The daemon inspects MP4 box structures in RAM in `< 1ms` (`ftyp`, explicit non-zero `mdat`, `moov` at EOF, 10,000-atom DoS ceiling), preventing incomplete transfers during active camera recording.
- **RAM Disk Staging (`/dev/shm`)**: Clips are extracted from the raw FAT32 image directly to RAM (`/dev/shm/arlo_staging`) with strict permissions (`0700` dir / `0600` file), completely eliminating wear and tear on the SBC's MicroSD card or eMMC storage.
- **Pure Monotonic Sequence Watermarking**: Arlo filenames strictly follow the pattern `<CAMERA_SERIAL>_<SEQUENCE_HEX>_<YYYYMMDD>_<HHMMSS>.mp4`. The daemon tracks only the latest sequence integer per camera serial in `/var/lib/arlo_sync_state.json`. Candidate file checks execute in $O(1)$ time with zero file-path caching or memory bloat.
- **Active Hex Directory Caching & Lookahead**: Arlo groups recordings into 6-character hex folders (`000000/`, `000001/`, rolling over approximately every 1,000 clips). The daemon caches active folders with a 30-second TTL (dynamically tightened to 5 seconds near folder rollover) and automatically scans the next sequential lookahead directory.
- **Format & Storage Reset Resilience**: If the Arlo Base Station reformats the USB drive (or if the user triggers a format in the Arlo app), the folder structure resets back to `000000`. The daemon detects when disk directory cursors regress, clears obsolete watermarks, and recovers automatically without manual intervention.
- **Atomic NAS Commit**: Clips stream into the Synology watch folder with a 1 MB buffer as hidden temporary files (`.<filename>.tmp`). Once verified against the source byte size, an atomic POSIX rename commits `<filename>.mp4`. `sightline-core` never reads half-transferred files.
- **Sub-Millisecond `mvhd` Duration Parsing**: Directly extracts the exact video duration from the MP4 Movie Header atom (`mvhd`) in `< 1ms` without spawning `ffprobe` or decoding video packets.
- **Detailed Latency Telemetry**: Logs granular micro-benchmarks for every synced clip: raw extraction time, validation time, SMB transfer speed, camera recording window, camera finalization delay, and total end-to-end sync lag.
- **Hardware `fsync` State Persistence**: Watermarks and directory cursors commit via atomic temporary write and hardware `os.fsync()`, ensuring state preservation across abrupt power cuts.
- **Hot Auto-Reload**: Watches the `arlo_sync.py` file on disk and re-executes itself in-place via `os.execv()` upon modifications without needing a systemd service restart.
- **Full Signal Trapping**: Safely traps `SIGINT`, `SIGTERM`, `SIGQUIT`, and `SIGHUP` to clean up RAM staging buffers and commit state before exiting.

---

## Hardware Requirements & Cabling

### 1. Single-Board Computer (SBC)

- **Tested & Verified Reference Board**: **Orange Pi Zero 2W** (4GB RAM)
  - **SoC**: Allwinner H618 (Quad-core Cortex-A53)
  - **Why this board**: The Orange Pi Zero 2W has an ultra-low power footprint (~150mA–250mA idle, ~350mA–500mA active load in headless mode). This allows it to run **100% bus-powered directly from the Arlo SmartHub's USB port** over a single USB cable—no external power brick, DC adapters, or splitter cables required.
  - **UDC Controller**: Native `musb-hdrc.4.auto` USB Device Controller supported out of the box in Armbian/Debian kernels.
- **Storage**: 8 GB+ MicroSD card (Class 10 / A1) or onboard eMMC.

> [!WARNING]
> **Can larger SBCs (Orange Pi 5, Raspberry Pi 4/5, etc.) be used?**
> **No, they cannot be powered by the Arlo SmartHub.** Arlo Base Station USB ports provide standard USB 2.0 power (~500 mA to 900 mA at 5V / 2.5W–4.5W). Full-sized boards like the Orange Pi 5, Orange Pi 3, or Raspberry Pi 4B/5 draw 2A to 4A (10W–20W) and will immediately brownout, boot-loop, or corrupt storage if connected directly. Additionally, larger boards often lack USB Device Controller (UDC) peripheral mode on standard USB ports.
>
> Other ultra-compact boards (e.g., Raspberry Pi Zero 2 W) may theoretically have low enough power draw, but **only the Orange Pi Zero 2W has been tested and verified** for this bridge.

### 2. Cabling & Connection

With the **Orange Pi Zero 2W**, installation is a clean **single-cable setup**:

1. Use a standard **USB-A to USB-C cable** (supporting both power and data).
2. Plug the **USB-A end** into the USB port on the back of the Arlo SmartHub or Base Station.
3. Plug the **USB-C end** into the **USB-C OTG port** on the Orange Pi Zero 2W.
4. The Arlo Base Station will simultaneously supply 5V bus power to the Orange Pi and recognize it as a USB flash drive.

---

## Step-by-Step Setup from Scratch

Follow these instructions to set up a clean single-board computer (Orange Pi, Raspberry Pi, etc.) running Armbian, Debian, or Ubuntu.

### Step 1: Install System Dependencies

Update the package index and install the required tools:

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    mtools \
    dosfstools \
    parted \
    cifs-utils \
    python3 \
    python3-pip
```

---

### Step 2: Create the Virtual USB Disk Image

Create the backing disk image file `/var/arlo_storage.img`. Arlo expects a drive formatted with a DOS/MBR partition table containing a single FAT32 partition.

A size between **4 GB and 16 GB** is ideal (large enough to buffer days of footage in case of network outages, while small enough to format instantly).

1. **Allocate the image file** (sparse allocation):
   ```bash
   # Create an 8 GB disk image
   sudo fallocate -l 8G /var/arlo_storage.img
   ```

2. **Partition the image as MBR / DOS with a primary FAT32 partition**:
   ```bash
   sudo parted -s /var/arlo_storage.img mklabel msdos mkpart primary fat32 2048s 100%
   ```

3. **Format Partition 1 as FAT32**:
   ```bash
   # Attach image as loop device with partition scanning
   sudo losetup -Pf /var/arlo_storage.img

   # Identify the loop device assigned (e.g. /dev/loop0)
   LOOP_DEV=$(losetup -j /var/arlo_storage.img | cut -d: -f1)

   # Format partition 1 with volume label "ARLO"
   sudo mkfs.vfat -F 32 -n "ARLO" "${LOOP_DEV}p1"

   # Detach the loop device
   sudo losetup -d "${LOOP_DEV}"
   ```

4. **Verify permissions**:
   ```bash
   sudo chmod 0666 /var/arlo_storage.img
   ```

---

### Step 3: Configure `mtools` for Raw Partition Access

Configure `/etc/mtools.conf` to assign drive letter `a:` to partition 1 of the virtual disk image:

```bash
sudo tee -a /etc/mtools.conf << 'EOF'

# Arlo USB Storage Bridge mapping
MTOOLS_SKIP_CHECK=1
drive a: file="/var/arlo_storage.img" partition=1
EOF
```

Test access to the virtual disk using `mdir`:
```bash
sudo mdir a:
```
*Expected output*: Volume label `ARLO` with free space listed.

---

### Step 4: Mount the Synology NAS Watch Folder via SMB/CIFS

Ensure the bridge SBC can write directly to the incoming watch directory used by `sightline-core` on your Synology NAS (e.g., `/docker/sightline-core/storage/watch` or `/surveillance/incoming`).

1. **Create a secure CIFS credentials file**:
   ```bash
   sudo mkdir -p /etc/synology
   sudo tee /etc/synology/cifs.creds << 'EOF'
   username=YOUR_SYNOLOGY_USERNAME
   password=YOUR_SYNOLOGY_PASSWORD
   EOF
   sudo chmod 600 /etc/synology/cifs.creds
   ```

2. **Create the mount point**:
   ```bash
   sudo mkdir -p /mnt/synology_watch
   ```

3. **Add persistent mount entry to `/etc/fstab`**:
   ```bash
   echo '//YOUR_SYNOLOGY_IP/docker/sightline-core/storage/watch /mnt/synology_watch cifs credentials=/etc/synology/cifs.creds,iocharset=utf8,_netdev,nofail,uid=0,gid=0,file_mode=0777,dir_mode=0777 0 0' | sudo tee -a /etc/fstab
   ```

4. **Mount and test write permissions**:
   ```bash
   sudo mount -a
   df -h /mnt/synology_watch
   touch /mnt/synology_watch/.test_write && rm /mnt/synology_watch/.test_write
   ```

---

### Step 5: Configure and Install the USB Gadget Service

The script `setup_usb_gadget.sh` sets up the USB composite gadget via Linux `configfs`, maps `/var/arlo_storage.img` to LUN 0 as a removable mass storage device, and binds to the board's USB Device Controller (UDC).

1. **Install the script to `/usr/local/bin`**:
   ```bash
   sudo cp setup_usb_gadget.sh /usr/local/bin/setup_usb_gadget.sh
   sudo chmod +x /usr/local/bin/setup_usb_gadget.sh
   ```

2. **Test gadget initialization manually**:
   ```bash
   sudo /usr/local/bin/setup_usb_gadget.sh
   ```
   *Expected output*: `[setup_usb_gadget] Gadget initialization complete.`

3. **Install and enable the systemd startup service**:
   ```bash
   sudo cp usb-gadget-init.service /etc/systemd/system/usb-gadget-init.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now usb-gadget-init.service
   sudo systemctl status usb-gadget-init.service
   ```

> [!TIP]
> **UDC Port Detection**: `setup_usb_gadget.sh` automatically checks `/sys/class/udc` to detect your SoC's device controller. On Allwinner boards (H2+/H3/H5/H6/H616), it binds to `musb-hdrc.4.auto` or `musb-hdrc.1.auto`. If your board uses a different UDC name (such as `fe980000.usb` or `dwc2`), it will be picked up automatically.

---

### Step 6: Connect to Arlo SmartHub & Format (If Required)

1. Connect the USB OTG cable from the bridge SBC to the USB port on the back of the Arlo SmartHub or Base Station.
2. Open the **Arlo Mobile App** or log in to the Arlo Web Portal.
3. Navigate to **Settings** → **My Devices** → Select your **SmartHub / Base Station** → **Storage Settings**.
4. You should see a USB drive labeled **"Available"** or **"Arlo Bridge Storage"**.
5. *(Optional but recommended)* Tap **Format USB Device** in the Arlo app to let the Base Station prepare its official directory layout.
   - The Arlo hub will format the drive and create the `/arlo` folder structure.
   - The sync daemon includes automatic format detection and will adjust its cursors seamlessly.

---

### Step 7: Install and Enable the Arlo Sync Daemon

1. **Install `arlo_sync.py` to `/usr/local/bin`**:
   ```bash
   sudo cp arlo_sync.py /usr/local/bin/arlo_sync.py
   sudo chmod +x /usr/local/bin/arlo_sync.py
   ```

2. **Install and enable the `arlo-sync.service`**:
   ```bash
   sudo cp arlo-sync.service /etc/systemd/system/arlo-sync.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now arlo-sync.service
   sudo systemctl status arlo-sync.service
   ```

---

### Step 8: Verify Real-Time Operation & Telemetry

Monitor the daemon's live log output:

```bash
sudo journalctl -u arlo-sync.service -f
```

Trigger motion on one of your Arlo cameras. Within seconds after the recording ends, you should observe log output similar to the following:

```text
[14:22:05] Arlo recording in progress for 52A1234567890_0001a4_20260912_142200.mp4: Unfinalized atom mdat (size 0 extends to EOF) at offset 32
[14:22:08] Synced & Verified: 52A1234567890_0001a4_20260912_142200.mp4 (4,194,304 bytes) [Camera: 52A1234567890, Seq: 420]
           └─ Breakdown: Extract: 18.2ms | Validate: 0.85ms | SMB Copy: 42.1ms (99.6 MB/s) | Cam Finalize: 1.20s (2 polls post-EOF) | Video: 12.0s | Cam Rec: 14:22:00 -> 14:22:12 | Sync Lag: 1.28s
```

---

## Understanding the Telemetry Breakdown

Each synchronized clip produces a structured log breakdown:

| Metric | Meaning | Expected Range |
| :--- | :--- | :--- |
| **Extract** | Time taken by `mcopy` to copy raw bytes from the FAT32 image to RAM staging (`/dev/shm`). | 10 ms – 50 ms |
| **Validate** | Time to parse top-level MP4 container boxes (`ftyp`, `mdat`, `moov`) in memory. | 0.4 ms – 1.2 ms |
| **SMB Copy** | Network transfer time and throughput streaming 1 MB blocks from RAM to the Synology share. | 30 ms – 120 ms (50–110 MB/s on Gigabit LAN) |
| **Cam Finalize** | Time elapsed between the video end timestamp and Arlo writing the final `moov` box. | 0.8 s – 2.5 s |
| **Polls post-EOF**| Number of 0.5s poll checks where Arlo was still closing/indexing the file. | 1 – 5 polls |
| **Video** | Exact video duration extracted from the MP4 `mvhd` atom. | e.g., 10.0s – 120.0s |
| **Cam Rec** | The camera's recording start time and calculated finish time. | Time range |
| **Sync Lag** | Total latency from when the camera finished recording to when the file landed on the NAS. | **< 1.5 seconds** |

---

## State Persistence & Automatic Recovery

The daemon maintains persistent tracking in `/var/lib/arlo_sync_state.json`:

```json
{
  "last_hex_dir": "000002",
  "watermarks": {
    "52A1234567890": 420,
    "52A9876543210": 118
  },
  "updated_at": "2026-09-12T14:22:08.123456"
}
```

- **Power-Failure Resilience**: Writes use a two-step temporary file swap (`.tmp` -> rename) coupled with physical disk flush (`os.fsync()`).
- **Zero Duplicate Transfers**: Even across daemon restarts or reboots, watermarks prevent re-syncing older clips.
- **Drive Format / Reset Detection**: If the Arlo hub reformats the USB drive, the disk folder resets from e.g. `000005` back to `000000`. The daemon immediately recognizes that `latest_disk_dir < last_recorded`, resets in-memory watermarks, and writes fresh state.

---

## File Summary

| File | Purpose |
| :--- | :--- |
| [`setup_usb_gadget.sh`](./setup_usb_gadget.sh) | Shell script configuring the composite USB mass storage gadget via `configfs` and `libcomposite`. |
| [`usb-gadget-init.service`](./usb-gadget-init.service) | Systemd oneshot unit executing `setup_usb_gadget.sh` at system initialization (`sysinit.target`). |
| [`arlo_sync.py`](./arlo_sync.py) | Main synchronization daemon: zero-mount extraction, in-memory MP4 validation, SMB streaming, and watermark management. |
| [`arlo-sync.service`](./arlo-sync.service) | Systemd persistent service managing `arlo_sync.py` with automatic restart on failure. |

---

## Maintenance & Operational Commands

### Restarting or Stopping Services

```bash
# Check status of the USB gadget
sudo systemctl status usb-gadget-init.service

# Check status of the sync daemon
sudo systemctl status arlo-sync.service

# Restart the sync daemon
sudo systemctl restart arlo-sync.service

# View live daemon logs
sudo journalctl -u arlo-sync.service -n 100 -f
```

### In-Place Hot Reloading

You do not need to restart the systemd service when updating `arlo_sync.py`. The running daemon continuously checks its own file modification timestamp (`mtime`). When you push or copy an updated `arlo_sync.py`, the daemon logs:

```text
[14:30:00] Script change detected (/usr/local/bin/arlo_sync.py). Reloading daemon in-place...
```
It saves its watermark state, purges RAM staging, and re-executes itself cleanly via `os.execv()`.

### Inspecting Raw Files with `mtools`

You can manually inspect the contents of the Arlo virtual drive without mounting:

```bash
# List top-level folders
sudo mdir a:/arlo

# List files in the latest active folder
sudo mdir a:/arlo/000000

# Manually extract a file to inspect it
sudo mcopy a:/arlo/000000/YOUR_CLIP.mp4 /tmp/test.mp4
```

---

## Extensibility: Bridging Other USB Storage Devices

While this daemon is configured out-of-the-box for Arlo Base Stations, the **underlying architecture is completely universal**:

1. **Hardware & Emulation Layer (`setup_usb_gadget.sh`)**:
   - The Linux USB Mass Storage Gadget (`libcomposite` + `configfs`) presents a standard USB flash drive to any host.
   - Any device that records or writes to a USB drive (e.g., **Blink Sync Module**) will recognize the bridge as a standard FAT32 drive.
2. **Zero-Mount Concurrency (`mtools`)**:
   - The zero-mount technique works identically for any device writing FAT32 sectors without kernel mounting conflicts.
3. **Adapting the Python Daemon for Other Systems**:
   - To adapt `arlo_sync.py` to a different camera hub or device, only two minor configuration changes are needed in the script:
     - **Directory Scanning**: Update `a:/arlo/{d}/*.mp4` to match your device's folder layout (e.g., `a:/blink/*.mp4`, `a:/DCIM/*.mp4`, or root `a:/*.mp4`).
     - **Filename Matching**: Adjust the regular expression `FILE_RE` to match the target device's filename format (e.g. timestamp-based filenames).
   - The in-memory MP4 validation (`is_valid_mp4`), RAM disk staging, atomic SMB streaming, and watermark persistence mechanisms apply seamlessly to any standard MP4 video clips.

---

## Troubleshooting Guide

### 1. `setup_usb_gadget.sh` Fails with "Device or resource busy" or "No such device"
- **Cause**: Kernel module `libcomposite` not loaded, or the USB port is configured in Host mode instead of Peripheral/OTG mode.
- **Fix**:
  - Verify `libcomposite` is loaded: `sudo modprobe libcomposite`.
  - Check UDC availability: `ls /sys/class/udc`. If empty, enable OTG mode in your device tree or boot configuration (e.g. in Armbian: `armbian-config` → System → Hardware, enable `usb-otg`, or verify `dr_mode = "otg"` / `dr_mode = "peripheral"` in DTS).

### 2. Arlo App Displays "USB Device Not Formatted" or "Drive Error"
- **Cause**: The disk image was created without a valid MBR partition table, or Arlo requires its own filesystem layout.
- **Fix**: Open the Arlo mobile app, navigate to **SmartHub Settings** → **Storage Settings**, and tap **Format USB Device**. The Arlo hub will write its preferred partition and directory structure. The daemon will automatically detect the format event and start syncing.

### 3. Arlo Recordings Repeatedly Log "Unfinalized atom mdat"
- **Cause**: Normal behavior while a camera is actively recording. Arlo keeps the file open and writes frames dynamically.
- **Fix**: No action needed. As soon as motion ceases and the camera stops recording, Arlo writes the `moov` atom and finalizes the file size. The daemon will validate and transfer the clip immediately.

### 4. Synology CIFS Mount Unavailable (`NAS mount unavailable. Retrying in 2s...`)
- **Cause**: Network disconnection, SMB credential error, or Synology DSM reboot.
- **Fix**:
  - Verify network connectivity: `ping YOUR_SYNOLOGY_IP`.
  - Test mounting manually: `sudo mount -v /mnt/synology_watch`.
  - Check CIFS credentials in `/etc/synology/cifs.creds`.
  - Ensure the DSM user has Read/Write permissions to the target shared folder.

### 5. Disk Space on the Bridge SBC (`/var/arlo_storage.img`)
- **Note**: The image file size on the bridge SBC does **not** grow indefinitely. Because it is a fixed virtual block device (e.g. 8 GB), Arlo manages its own capacity. When the drive fills up, Arlo's automatic overwrite mechanism deletes the oldest video files in the earliest hex folders (`000000`, `000001`, etc.) to make room for new recordings. Because the daemon tracks monotonic sequence numbers per camera rather than relying on folder history, Arlo's file pruning never disrupts the sync pipeline.
