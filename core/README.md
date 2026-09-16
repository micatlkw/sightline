# sightline-core

Event-driven AI surveillance engine and Web Dashboard for home NAS (Synology DSM / Linux).

Sightline monitors a directory for incoming `.mp4` video clips dropped by camera bridges or IP cameras, runs CPU-optimized YOLO object detection on keyframes, archives clips into date-partitioned storage, logs events to SQLite, dispatches push notifications across multiple channels, and provides a responsive Web Dashboard (PWA) with real-time WebSocket alerts, seekable video playback, and live settings management.

---

## Disclaimer & Security Notice

- **Personal Hobby Project**: Sightline was developed by an independent hobbyist for personal home surveillance needs. It is not an enterprise-grade or commercially audited security platform.
- **Best-Effort Hardening**: Great care has been taken to design Sightline with privacy and security in mind—including zero open router ports via Cloudflare Tunnel, Google SSO authentication, LAN subnet verification, CSRF validation on mutating endpoints, unprivileged non-root container execution (`PUID`/`PGID`), and internal TLS encryption. However, no software is impenetrable, and it has not been subjected to formal third-party penetration testing or commercial audits.
- **Use at Your Own Risk**: This software is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE, as expressed in the [GNU Affero General Public License](file:///volume1/sightline/github/sightline/LICENSE). The author assumes no liability or responsibility for missed detections, false alarms, system downtime, network exposure, data loss, or security incidents resulting from the use of this project.
- **Vulnerability Reporting & Community Feedback**: If you discover a security vulnerability or have suggestions for hardening, please report it responsibly using **GitHub Private Vulnerability Reporting** (under the repository's Security tab) or open a GitHub Issue for general bugs and enhancements.

---

## Architecture Overview

```
                      [ Camera Bridge / IP Cameras / FTP / SMB ]
                                          │
                                          ▼  (.mp4 clips)
                                  /data/incoming
                                          │
                        ┌─────────────────┴─────────────────┐
                        │   inotify / Polling Watcher       │
                        │   (stability check: stable size)  │
                        └─────────────────┬─────────────────┘
                                          │
                                          ▼
                        ┌───────────────────────────────────┐
                        │   Inference Pipeline (Multi-worker)
                        │   • Adaptive Keyframe Extraction  │
                        │   • CPU-Optimized Ultralytics YOLO│
                        │   • Bounding Boxes & Confidence   │
                        └─────────┬───────────────┬─────────┘
                                  │               │
                     (Detections) │               │ (All clips)
                                  ▼               ▼
        ┌───────────────────────────────┐   ┌───────────────────────────────┐
        │  SQLite DB & Thumbnails       │   │  Processed Archive            │
        │  • sightline.db               │   │  /data/processed/YYYY-MM-DD/  │
        │  • Animated GIF / JPEG thumbs │   │    <Camera>/<file>-<objs>.mp4 │
        └───────────────┬───────────────┘   └───────────────────────────────┘
                        │
                        ▼
        ┌───────────────────────────────────────────────────────────────────┐
        │  Multi-Channel Notification Dispatcher                            │
        │  • Native Web Push (PWA, VAPID, RFC 8030 topic collapsing, deep link)
        │  • Apprise (ntfy, Pushover, Discord, Telegram, Slack, etc.)       │
        │  • Firebase Cloud Messaging (FCM for companion mobile apps)       │
        └───────────────────────────────────────────────────────────────────┘
                                          │
                        ┌─────────────────┴─────────────────┐
                        │  FastAPI Backend (Port 8000/8080) │
                        │  • REST API (/api/v1/...)         │
                        │  • WebSocket live feed (/ws/events)│
                        │  • HTTP 206 Partial Content video │
                        └─────────────────┬─────────────────┘
                                          │
                        ┌─────────────────┴─────────────────┐
                        │  Caddy Reverse Proxy (Port 4210)  │
                        │  • Auto-reloading TLS (LAN/ACME)  │
                        │  • Security headers & Stealth 404 │
                        └─────────────────┬─────────────────┘
                                          │
                 ┌────────────────────────┴────────────────────────┐
                 ▼                                                 ▼
        [ Web Dashboard (PWA) ]                         [ Cloudflare Tunnel ]
        (Desktop / Mobile Safari / Chrome)              (Optional WAN Access)
```

---

## Key Features

- **Zero-Latency Ingestion**: True Linux `inotify` file watcher for instant clip detection with stable file-size validation; optional polling mode for NFS/SMB network mounts.
- **CPU-Optimized YOLO Detection**: Powered by Ultralytics YOLO (supports YOLO11, YOLOv8, YOLO26). Built-in cryptographic SHA-256 verification for model weights.
- **Adaptive Keyframe Sampling**: Configure keyframes per second (`sample_fps`) or stride (`vid_stride`), with automatic frame-rate boost for short clips (`min_clip_keyframes`).
- **Granular Detection Tuning**: Filter by COCO class names (`person`, `car`, `dog`, etc.) or IDs. Set global, per-class (`class_confidence_thresholds`), or per-camera overrides.
- **Full-Featured Web Dashboard (PWA)**:
  - Real-time detection event stream over WebSocket (`/ws/events`).
  - Seekable HTML5 MP4 video player with HTTP 206 partial content streaming.
  - Animated 3-frame keyframe GIF thumbnails.
  - Multi-select filtering by date, camera, and detected object class.
  - Batch event deletion, processed archive re-indexing, and missing-record pruning.
  - Interactive settings editor with live hot-reloading (no container restart required).
  - Installable PWA with offline caching for iOS Safari and Android Chrome.
- **Triple-Tier Notification Engine**:
  - **Native Web Push (PWA)**: Browser push notifications with auto-generated VAPID keys, tap-to-play deep linking directly to the video, and RFC 8030 camera topic collapsing to prevent notification flood.
  - **Apprise**: Push alerts to 80+ notification backends (ntfy, Pushover, Telegram, Discord, Slack, Gotify, email, etc.).
  - **Firebase Cloud Messaging (FCM)**: Push alerts to companion native mobile apps.
  - **Per-Camera Cooldown**: Suppresses alert storms when continuous activity occurs.
- **Security & Authentication**:
  - Google SSO authentication with whitelisted accounts (`allowed_google_emails`) and admin permissions (`admin_google_emails`).
  - Seamless LAN authentication bypass (`allow_lan_auth_bypass: true`) for trusted home subnets (192.168.x.x, 10.x.x.x, 172.16-31.x.x, 127.0.0.1).
  - Container dynamic privilege-drop entrypoint (`PUID`/`PGID`) preventing Synology/Linux file ownership conflicts.
  - Hardened Caddy reverse proxy with automatic internal TLS for LAN, Let's Encrypt / ZeroSSL for public domains, custom cert support, and stealth 404 blocking of internal management endpoints over WAN.
  - Optional Cloudflare Tunnel integration (`cloudflared`) for secure remote access without opening router ports.

---

## Directory Structure

Sightline expects the following persistent directory layout on your host:

```
<BASE_DIR>/
├── incoming/             # Cameras drop raw .mp4 clips here
├── data/
│   ├── sightline.db      # SQLite database (events, clips, device tokens)
│   ├── thumbnails/       # Keyframe GIF/JPEG thumbnails
│   └── processed/        # Date-partitioned archive (YYYY-MM-DD/<Camera>/...)
├── models/               # Cached YOLO model weights (.pt) & Firebase credentials
├── config/
│   ├── settings.yaml     # Application configuration (hot-reloaded)
│   └── Caddyfile         # Auto-generated by sightline-core for Caddy
└── caddy_data/           # Persistent TLS certificates (internal CA & ACME)
```

> **Host Path Default**: On Synology DSM, `<BASE_DIR>` is typically `/volume1/sightline`. On a generic Linux host, this can be `./data` or `/opt/sightline`.

---

## Step-by-Step Setup from Scratch

### 1. Clone the Repository

```bash
git clone https://github.com/micatlkw/sightline.git
cd sightline/core
```

### 2. Identify Your User & Group ID (PUID / PGID)

To prevent permission conflicts when Sightline reads incoming camera clips and writes database/processed files, determine your host user and group ID:

```bash
id -u   # Example: 1026 (Synology) or 1000 (Linux)
id -g   # Example: 100  (Synology users group) or 1000 (Linux)
```

### 3. Create Required Host Directories

Create the persistent directories on your host and grant ownership to your `PUID:PGID`:

**On Synology NAS (`/volume1/sightline`):**
```bash
sudo mkdir -p /volume1/sightline/{incoming,data,models,config,caddy_data}
sudo chown -R $(id -u):$(id -g) /volume1/sightline
```

**On Generic Linux (`./sightline-storage` or local directory):**
If you prefer storing data in a local folder, create the directories and adjust `docker-compose.yml` accordingly:
```bash
mkdir -p ./sightline-storage/{incoming,data,models,config,caddy_data}
```

### 4. Configure Environment Variables (`.env`)

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` to match your environment:

```dotenv
# ── Process Privileges (Non-Root) ─────────────────────────────────────────────
PUID=1026             # Set to your user ID (id -u)
PGID=100              # Set to your group ID (id -g)

# ── Reverse Proxy Ports (Caddy) ──────────────────────────────────────────────
# Host ports exposed by Caddy reverse proxy (default 4210/4280 avoids Synology DSM 443/80)
HTTPS_PORT=4210
HTTP_PORT=4280

# ── Storage Paths (inside container) ──────────────────────────────────────────
INCOMING_DIR=/data/incoming
PROCESSED_DIR=/data/processed
DB_PATH=/data/sightline.db
MODELS_DIR=/models
THUMBNAILS_DIR=/data/thumbnails

# ── Detection Defaults ────────────────────────────────────────────────────────
YOLO_MODEL=yolo11n.pt
TARGET_CLASSES=[0,2,3,7,15,16,21]
CONFIDENCE_THRESHOLD=0.45
VID_STRIDE=30

# ── Ingestion / Watcher ───────────────────────────────────────────────────────
# false = inotify (recommended for local filesystem / Synology NAS)
# true  = polling fallback (required if watch dir is an NFS/SMB network mount)
WATCH_USE_POLLING=false
CLIP_STABLE_SECONDS=2.0

# ── API & Authentication ──────────────────────────────────────────────────────
API_HOST=0.0.0.0
API_PORT=8000
ALLOWED_GOOGLE_EMAILS=[]

# ── Notifications ─────────────────────────────────────────────────────────────
ALERT_COOLDOWN_SECONDS=60
APPRISE_URLS=[]
FIREBASE_CREDENTIALS_PATH=/models/firebase_credentials.json

# ── Cloudflare Tunnel (Optional) ──────────────────────────────────────────────
# Leave empty if not using Cloudflare Tunnel
TUNNEL_TOKEN=
```

### 5. Review `docker-compose.yml`

Inspect `docker-compose.yml`:
- **Volume mounts**: Default mounts point to `/volume1/sightline/...`. If your host paths differ, update the left side of the volume bindings.
- **Development live mount**: `./app:/app/app` mounts the local `app` directory for hot-reloading code changes without rebuilding the image. For a production deployment without mounting source code, you can comment this line out.
- **Cloudflare Tunnel (`cloudflared`)**: If you are not using Cloudflare Tunnel, comment out the `cloudflared` service block.

### 6. (Optional) Pre-seed `settings.yaml`

Sightline will auto-generate `/config/settings.yaml` on first boot if it does not exist. Alternatively, you can copy the example file to your config volume before starting:

```bash
cp settings.yaml.example /volume1/sightline/config/settings.yaml
```

You can customize camera definitions, notification URLs, and Google SSO emails directly in `settings.yaml` (see [Configuration Reference](#configuration-reference) below).

### 7. Build and Start the Containers

```bash
docker compose up -d --build
```

Monitor the startup logs:

```bash
docker compose logs -f sightline-core
```

You should see:
1. Dynamic privilege-drop dropping permissions to `PUID:PGID`.
2. Database connected and schema migrations applied.
3. YOLO model loaded (auto-downloaded from Ultralytics on first run into `models/`).
4. Inotify directory watcher active on `/data/incoming`.
5. Caddy reverse proxy started with internal self-signed TLS.
6. Web UI Dashboard ready.

### 8. Verify Health & Open Dashboard

- **Local Host API Health Check**:
  ```bash
  curl http://127.0.0.1:8000/health
  ```
  Returns `{"status": "ok", "watcher": {"alive": true, ...}, "detection": {...}}`.

- **Web Dashboard**:
  Open in your web browser:
  ```
  https://<nas-ip>:4210
  ```
  *(Or `http://<nas-ip>:4280`, which proxies directly to the Web UI on local LAN)*.
  > **Note on TLS warning**: When accessing via LAN IP using Caddy's internal CA, your browser will display a self-signed certificate warning. Accept the warning to proceed to the dashboard.

---

## Verifying Detection with a Test Clip

1. Prepare or download any short `.mp4` video clip (e.g. containing a person, car, or pet).
2. Name the clip following the camera naming convention:
   ```bash
   cp test.mp4 /volume1/sightline/incoming/FRONTDOOR_00000001_20260912_120000.mp4
   ```
3. Watch the pipeline logs:
   ```bash
   docker compose logs -f sightline-core
   ```
   You will observe:
   - Inotify detects the new file.
   - File size stability check confirms writing is complete.
   - YOLO inference processes keyframes.
   - Detections, confidence scores, and bounding boxes are logged.
   - Keyframe GIF thumbnail is generated in `/data/thumbnails/`.
   - Event record saved in SQLite database.
   - Notifications dispatched to all subscribed channels.
   - Clip is moved to `/data/processed/YYYY-MM-DD/FRONTDOOR/FRONTDOOR_00000001_20260912_120000-1person.mp4`.
4. Refresh the Web UI at `https://<nas-ip>:4210` to view the event card, animated GIF, and click to play the full video!

Alternatively, you can manually enqueue an existing clip via the API:
```bash
curl -X POST http://127.0.0.1:8000/api/v1/clips/process \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/incoming/test.mp4"}'
```

---

## Web Dashboard & Progressive Web App (PWA)

Sightline includes a built-in, responsive web application served directly from the container:

- **Live WebSocket Event Stream**: Detects new activity in real time and automatically prepends new event cards without refreshing.
- **Seekable Video Playback**: Built-in HTML5 video modal supporting HTTP 206 Range requests for instant scrubbing across large video files.
- **Smart Filtering**: Filter events by date range, camera name, and detected object class.
- **Event Management**: Single-click and batch deletion of event records and associated video/thumbnail files.
- **Interactive Settings Management**: View and modify camera mappings, detection thresholds, sampling rates, and edit the raw `settings.yaml` configuration with live reloading.
- **Installable PWA**:
  - **iOS**: Open in Safari → Tap Share icon → **Add to Home Screen**.
  - **Android / Desktop**: Open in Chrome/Edge → Tap the install button in the address bar or browser menu → **Install Sightline**.

---

## Notification Setup

Sightline supports three notification delivery channels that can be used simultaneously:

### 1. Native Web Push (PWA) — Recommended

Receive instant notifications directly on your smartphone (Android and iOS 16.4+) or desktop browser with zero third-party cloud dependencies:
1. Open the Web Dashboard at `https://<your-host>:4210` or your public domain.
2. Click the **Notification Bell** icon in the dashboard header.
3. Grant notification permissions when prompted by your browser.
4. Click **Send Test Push** to verify delivery.
5. **Tap-to-Play**: Tapping any notification opens the dashboard and jumps directly to the recorded video.
6. **Burst Collapsing**: Multiple alerts from the same camera are automatically collapsed into a single notification tray item via RFC 8030 topic headers (`webpush_topic_mode: "camera"`).

### 2. Apprise (Multi-Service Push)

Push alerts to 80+ messaging and notification platforms. Configure `apprise_urls` in `settings.yaml` (or `APPRISE_URLS` in `.env` as a JSON array):

```yaml
apprise_urls:
  # ntfy (free, open source, Android/iOS app, self-hosted or ntfy.sh)
  - "ntfys://ntfy.sh/my-secret-topic?priority=high&tags=rotating_light,camera"

  # Pushover (Android/iOS/Desktop)
  - "pover://UserKey@AppToken/"

  # Discord Webhook
  - "discord://webhook_id/webhook_token"

  # Telegram Bot
  - "tgram://bot_token/chat_id"

  # Slack Webhook
  - "slack://TokenA/TokenB/TokenC/Channel"

  # Gotify (Self-hosted push)
  - "gotifys://gotify.example.com/app_token"
```

### 3. Firebase Cloud Messaging (FCM)

For companion native mobile apps:
1. Download your Firebase service account private key JSON from the Firebase Console.
2. Save it to `/volume1/sightline/models/firebase_credentials.json`.
3. Mobile devices register their FCM tokens via `POST /api/v1/devices/register`.

---

## Camera Matching & Filename Conventions

Sightline determines the camera name and serial using the following priority:
1. **Filename prefix before the first underscore**:
   - `CAM0100000001_000000e2_20260828_122954.mp4` → Camera Serial: `CAM0100000001`
2. **Subdirectory under incoming directory**:
   - `incoming/Backyard/clip_001.mp4` → Camera: `Backyard`
3. **Parent directory name**:
   - Used when not a generic folder name (`incoming`, `processed`, `corrupt`, etc.).

### Mapping Serials to Human-Readable Names

Define friendly camera names and per-camera detection overrides in `settings.yaml`:

```yaml
cameras:
  - name: "Backyard"
    serial: "CAM0100000001"
    enabled: true
    target_classes: ["person", "dog", "cat", "bear"]
    confidence_threshold: 0.45
    cooldown_seconds: 30
    sample_fps: 1.5

  - name: "Driveway"
    serial: "CAM0200000002"
    enabled: true
    target_classes: ["person", "car", "truck", "motorcycle"]
    confidence_threshold: 0.50
    cooldown_seconds: 45
    sample_fps: 1.0
```

Unspecified camera properties automatically inherit the global defaults.

---

## Configuration Reference

Sightline uses a two-tier configuration model:
1. **`.env`**: Sets bootstrap container variables (user permissions, ports, container volume paths).
2. **`settings.yaml`**: Drives application runtime behavior (cameras, detection thresholds, notifications, auth, reverse proxy). **Changes to `settings.yaml` are hot-reloaded automatically without restarting containers.**

### Environment Variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `PUID` | `1026` | Host user ID for dynamic privilege drop (run `id -u`) |
| `PGID` | `100` | Host group ID for dynamic privilege drop (run `id -g`) |
| `HTTPS_PORT` | `4210` | Host port for Caddy HTTPS reverse proxy |
| `HTTP_PORT` | `4280` | Host port for Caddy HTTP-to-HTTPS redirect & LAN access |
| `API_HOST` | `0.0.0.0` | Bind address for internal FastAPI server |
| `API_PORT` | `8000` | Port for internal FastAPI server (mapped to `127.0.0.1:8000`) |
| `INCOMING_DIR` | `/data/incoming` | Watched directory for incoming `.mp4` camera clips |
| `PROCESSED_DIR` | `/data/processed` | Destination archive directory for processed clips |
| `DB_PATH` | `/data/sightline.db` | SQLite database file path |
| `MODELS_DIR` | `/models` | Directory for cached YOLO weights and credentials |
| `THUMBNAILS_DIR` | `/data/thumbnails` | Output directory for keyframe thumbnails |
| `YOLO_MODEL` | `yolo11n.pt` | Default YOLO model file (auto-downloaded if absent) |
| `TARGET_CLASSES` | `[0,2,3,7,15,16,21]` | Default COCO class IDs to detect |
| `CONFIDENCE_THRESHOLD` | `0.45` | Global minimum detection confidence (0.01 – 1.0) |
| `VID_STRIDE` | `30` | Frame stride interval (30 = 1 frame/sec at 30 fps) |
| `WATCH_USE_POLLING` | `false` | Set `true` for NFS/SMB network mounts; `false` for inotify |
| `CLIP_STABLE_SECONDS` | `2.0` | Seconds of unchanging file size before processing |
| `ALERT_COOLDOWN_SECONDS` | `60` | Minimum seconds between alerts per camera |
| `ALLOWED_GOOGLE_EMAILS` | `[]` | Whitelisted Google accounts for SSO (JSON array) |
| `FIREBASE_CREDENTIALS_PATH` | `/models/firebase_credentials.json` | Path to Firebase service account JSON |
| `APPRISE_URLS` | `[]` | Push notification service URLs (JSON array) |
| `TUNNEL_TOKEN` | `""` | Optional Cloudflare Tunnel token |

### Settings Configuration (`settings.yaml`)

Key options available in `settings.yaml` (see [`settings.yaml.example`](settings.yaml.example) for the full annotated file):

```yaml
# ── Global Detection Defaults ─────────────────────────────────────────────────
yolo_model: yolo11n.pt
confidence_threshold: 0.45
sample_fps: 1.0              # Keyframes evaluated per second of video
min_clip_keyframes: 5        # Guaranteed minimum keyframes for short clips

target_classes:
  - person
  - car
  - motorcycle
  - truck
  - cat
  - dog
  - bear

# Optional per-class confidence threshold overrides
class_confidence_thresholds:
  person: 0.45
  dog: 0.55
  cat: 0.50

# ── Camera Definitions ────────────────────────────────────────────────────────
cameras:
  - name: "Backyard"
    serial: "CAM0100000001"
    enabled: true
    target_classes: ["person", "dog", "bear"]

# ── Notifications ─────────────────────────────────────────────────────────────
alert_cooldown_seconds: 60
apprise_urls: []
firebase_credentials_path: "/models/firebase_credentials.json"
webpush_topic_mode: "camera"   # "camera" (per-camera collapsing) or "global"
webpush_ttl_seconds: 86400

# ── Watcher & Ingestion ───────────────────────────────────────────────────────
clip_stable_seconds: 2.0
scan_on_startup: true
watch_use_polling: false
pipeline_concurrency: 2        # Concurrent clip processing threads

# ── Reverse Proxy & TLS (Caddy) ───────────────────────────────────────────────
https_port: 8443               # Host port inside Caddy (maps to HTTPS_PORT in .env)
http_port: 8080                # Host port inside Caddy (maps to HTTP_PORT in .env)
domain_name: null              # Set "cam.yourdomain.com" for Let's Encrypt
acme_email: null               # Renewal notification email for ACME

# ── Authentication (Google SSO) ───────────────────────────────────────────────
allowed_google_emails: []      # Empty list = open access / bypassed on LAN
admin_google_emails: []        # Admins permitted to edit settings/delete events
google_client_id: null         # Optional OAuth Client ID
allow_lan_auth_bypass: true    # Bypass Google login when accessing from home LAN
```

---

## HTTPS, TLS & Remote Access (Caddy)

Sightline includes an integrated Caddy reverse proxy container (`caddy:2-alpine`). Caddy's configuration is automatically generated and updated by Sightline whenever `settings.yaml` changes.

### 1. Local LAN Mode (Default)
- When `domain_name` is omitted, Caddy automatically provisions self-signed TLS certificates using its internal CA (`tls internal`).
- Accessible via HTTPS at `https://<nas-ip>:4210` or plain HTTP at `http://<nas-ip>:4280`.
- LAN clients bypass Google SSO when `allow_lan_auth_bypass: true`.

### 2. Public Domain & Let's Encrypt
- Set `domain_name: "cam.yourdomain.com"` and `acme_email: "admin@yourdomain.com"` in `settings.yaml`.
- Forward external ports 443/80 on your router to your host's `HTTPS_PORT` / `HTTP_PORT`.
- Caddy automatically obtains and renews valid public TLS certificates via ACME.

### 3. Custom Host Certificates (e.g. Synology DSM Certs)
- Mount or place your `.crt` and `.key` files (e.g. `/volume1/sightline/config/certs/`).
- Set `ssl_cert_path` and `ssl_key_path` in `settings.yaml`.

### 4. Cloudflare Tunnel (`cloudflared`)
- For secure remote access without port forwarding or exposing your home IP:
- Create a Cloudflare Tunnel in the Zero Trust dashboard pointing to `https://caddy:4210` with NoTLSVerify enabled.
- Paste your token into `TUNNEL_TOKEN` in `.env`.

---

## API Reference

Direct host API base URL: `http://127.0.0.1:8000` (or `https://<nas-ip>:4210` via reverse proxy).
All routes are available with `/api/v1` prefix or at root.

> **Security Note**: Internal administration endpoints (`/docs`, `/clips/process`, `/settings`, `/health`) are blocked by Caddy over WAN with stealth 404 responses. Access them locally on port 8000 or through authenticated Web UI sessions.

### System & Health
```http
GET  /health                         # System liveness, pipeline stats, watcher status
POST /api/v1/health/notification-test # Send test notification across all providers
```

### Detection Events
```http
GET    /api/v1/events                # List events (?camera=Backyard&cls=person&limit=50&offset=0)
GET    /api/v1/events/{id}           # Get single event details
GET    /api/v1/events/{id}/thumbnail # Stream animated GIF / JPEG keyframe thumbnail
GET    /api/v1/events/{id}/video     # Stream MP4 video (HTTP 206 Partial Content)
DELETE /api/v1/events/{id}           # Delete event and associated files (Admin)
POST   /api/v1/events/{id}/delete    # POST alias for delete
POST   /api/v1/events/batch-delete   # Batch delete events by ID (Admin)
GET    /api/v1/events/filters        # Get available dates, cameras, and classes for UI
POST   /api/v1/events/rescan         # Rescan processed archive, sync database, prune missing
```

#### Event Object Schema
```json
{
  "id": 42,
  "clip_path": "CAM0100000001_000000e2_20260828_122954-1person-1car.mp4",
  "clip_filename": "CAM0100000001_000000e2_20260828_122954-1person-1car.mp4",
  "video_url": "/api/v1/events/42/video",
  "camera_name": "Backyard",
  "detected_at": "2026-08-28T12:29:54Z",
  "thumbnail_url": "/api/v1/events/42/thumbnail",
  "objects": [
    {
      "class": "person",
      "class_id": 0,
      "confidence": 0.87,
      "timestamp_sec": 3.0,
      "bbox": [0.1, 0.2, 0.5, 0.8]
    },
    {
      "class": "car",
      "class_id": 2,
      "confidence": 0.71,
      "timestamp_sec": 7.0,
      "bbox": [0.4, 0.3, 0.9, 0.7]
    }
  ]
}
```

### Clip Processing History
```http
GET  /api/v1/clips                   # List processing queue history
POST /api/v1/clips/process           # Manually enqueue a clip: {"path": "/data/incoming/test.mp4"}
POST /api/v1/clips/scan              # Scan incoming directory for unprocessed clips
```

### Web Push & Notifications
```http
GET  /api/v1/notifications/vapid-public-key # Get VAPID public key for Web Push subscription
POST /api/v1/notifications/subscribe        # Register browser Web Push subscription
POST /api/v1/notifications/unsubscribe      # Remove browser Web Push subscription
POST /api/v1/notifications/test-push        # Send immediate test push to current user
GET  /api/v1/notifications/status           # Check push subscription status
POST /api/v1/notifications/test             # Test all configured notification backends
```

### Mobile Devices & Preferences
```http
POST   /api/v1/devices/register      # Register mobile FCM token
GET    /api/v1/devices               # List registered devices for current user
DELETE /api/v1/devices/{device_id}   # Unregister mobile device
GET    /api/v1/preferences           # Get per-camera alert preferences
PUT    /api/v1/preferences           # Update camera mute switch / alert toggle
```

### Settings & Configuration
```http
GET  /api/v1/settings                # Get active application settings
PUT  /api/v1/settings                # Update configuration
GET  /api/v1/settings/models         # List cached YOLO models in /models
PUT  /api/v1/settings/raw            # Update settings via raw YAML payload
```

### Authentication (Google SSO)
```http
GET  /api/v1/auth/status             # Check SSO configuration, LAN status, admin role
POST /api/v1/auth/login              # Exchange Google ID token for secure session cookie
POST /api/v1/auth/logout             # Revoke current session token and clear cookie
GET  /api/v1/auth/me                 # Get authenticated user profile
POST /api/v1/auth/revoke-other-sessions # Invalidate other sessions for current account
POST /api/v1/auth/revoke-user-sessions  # Invalidate all sessions for specified user (Admin)
```

### Real-Time WebSocket
```http
WS /ws/events                        # Real-time detection event stream (?token=<google_id_token>)
```

Connect with any WebSocket client:
```bash
# Using websocat (https://github.com/vi/websocat)
websocat ws://localhost:8000/ws/events
```

---

## Detection Classes Reference

Sightline supports all standard 80 COCO classes by name (e.g. `"person"`) or integer ID. Common surveillance classes:

| COCO ID | Label | COCO ID | Label |
|---|---|---|---|
| `0` | person | `7` | truck |
| `1` | bicycle | `14` | bird |
| `2` | car | `15` | cat |
| `3` | motorcycle | `16` | dog |
| `5` | bus | `21` | bear |

Set target classes in `settings.yaml` using human-readable names:
```yaml
target_classes:
  - person
  - car
  - dog
  - cat
```

---

## Troubleshooting & FAQ

### Permission Denied on `/data` or `.mp4` clips not moving
- Check your host `PUID` and `PGID` using `id -u` and `id -g`.
- Ensure `.env` contains `PUID=<id>` and `PGID=<group_id>`.
- Verify the host directory ownership:
  ```bash
  sudo chown -R $(id -u):$(id -g) /volume1/sightline
  ```

### Clips not detected on network shares (NFS / SMB)
- Linux `inotify` events are not propagated across remote network filesystems.
- Set `watch_use_polling: true` in `settings.yaml` (or `WATCH_USE_POLLING=true` in `.env`).

### Port conflicts on Synology NAS (Port 80 / 443)
- Synology DSM's internal Nginx reserves ports 80 and 443 for system services.
- Sightline's Caddy defaults to `HTTPS_PORT=4210` and `HTTP_PORT=4280`. You can change these to any unused ports in `.env`.

### Self-Signed Certificate Warnings on Local LAN
- In Local LAN mode, Caddy generates an internal self-signed TLS certificate (`tls internal`).
- This is normal for local private IP access (`192.168.x.x`). Simply click **Advanced → Proceed** in your browser, or configure a public domain with Let's Encrypt / Cloudflare Tunnel for full green-padlock TLS.

### Downloading or switching YOLO models
- When changing `yolo_model` (e.g. from `yolo11n.pt` to `yolo11s.pt` or `yolov8n.pt`), the model weights will be automatically downloaded by Ultralytics on container startup and saved in the persistent `/models` volume.

