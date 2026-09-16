# Sightline: Privacy-First AI Surveillance System & Web Dashboard

Sightline is an end-to-end, event-driven home surveillance platform designed for **Synology NAS (DSM) and Linux servers**. It performs real-time, CPU-optimized YOLO AI object detection on incoming camera video clips, provides instant mobile push alerts with video snapshot previews, and delivers a responsive, seekable Web Dashboard (PWA) with **zero open router ports**.

---

## Disclaimer & Security Notice

- **Personal Hobby Project**: Sightline was developed by an independent hobbyist for personal home surveillance needs. It is not an enterprise-grade or commercially audited security platform.
- **Best-Effort Hardening**: Great care has been taken to design Sightline with privacy and security in mind—including zero open router ports via Cloudflare Tunnel, Google SSO authentication, LAN subnet verification, CSRF validation on mutating endpoints, unprivileged non-root container execution (`PUID`/`PGID`), and internal TLS encryption. However, no software is impenetrable, and it has not been subjected to formal third-party penetration testing or commercial audits.
- **Use at Your Own Risk**: This software is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE, as expressed in the [GNU Affero General Public License](file:///volume1/sightline/github/sightline/LICENSE). The author assumes no liability or responsibility for missed detections, false alarms, system downtime, network exposure, data loss, or security incidents resulting from the use of this project.
- **Vulnerability Reporting & Community Feedback**: If you discover a security vulnerability or have suggestions for hardening, please report it responsibly using **GitHub Private Vulnerability Reporting** (under the repository's Security tab) or open a GitHub Issue for general bugs and enhancements.

---

## Key Highlights

- ⚡ **Zero-Latency Ingestion**: True Linux `inotify` file watcher detects incoming `.mp4` video clips immediately with stable file-size validation; optional polling mode for NFS/SMB network mounts.
- 🧠 **CPU-Optimized YOLO AI Detection**: Runs locally on your CPU (supports YOLO11, YOLOv8, and YOLO26). Automatically downloads and cryptographically verifies model weights from Ultralytics releases.
- 📱 **Responsive Web Dashboard & Progressive Web App (PWA)**: Modern web interface with real-time WebSocket event updates, seekable HTML5 video playback with HTTP 206 Range streaming, animated GIF keyframe previews, and interactive settings editing. Installable directly to the home screen on iOS Safari and Android Chrome.
- 🔔 **Triple-Tier Notification Engine**:
  - **Native Web Push (PWA)**: Zero-cloud-dependency browser push notifications with auto-generated VAPID keys, tap-to-play deep linking, and RFC 8030 camera topic collapsing to prevent notification flood.
  - **Apprise**: Push alerts to 80+ notification services (ntfy, Pushover, Telegram, Discord, Slack, Gotify, etc.).
  - **Firebase Cloud Messaging (FCM)**: Push alerts for companion mobile applications.
- 🔒 **Zero-Port Remote Access**: Hardened Caddy reverse proxy providing automatic internal TLS for local LAN and seamless Cloudflare Tunnel (`cloudflared`) integration for worldwide access without opening router ports.
- 🔌 **Hardware Arlo SmartHub Bridge**: Includes a zero-mount USB storage emulation daemon for Linux SBCs (Orange Pi / Raspberry Pi) to extract recordings from proprietary Arlo base stations with sub-second handoff latency.

---

## Repository Structure

- [`core/`](./core): The core surveillance engine, FastAPI backend, YOLO inference pipeline, inotify watcher, Caddy reverse proxy, and built-in Web Dashboard (PWA). See [`core/README.md`](./core/README.md) for full configuration and API references.
- [`bridge/`](./bridge): Hardware USB storage bridge daemon for Arlo SmartHubs / Base Stations (Linux USB Gadget, zero-mount FAT32 reader, sub-second SMB sync). See [`bridge/README.md`](./bridge/README.md) for hardware setup and wiring guides.

---

## Architecture Overview

```
[ Arlo Bridge / IP Cameras / FTP / SMB Drop ]
                      │
                      ▼  (.mp4 video clip)
              /data/incoming/
                      │
                      ▼
         [ sightline-core (Docker) ]
           ├── inotify Watcher (File stability validation)
           ├── Adaptive Keyframe Extraction
           ├── CPU-Optimized Ultralytics YOLO Inference
           ├── Keyframe GIF / JPEG Thumbnail Generation
           ├── Date-Partitioned Archiving (/data/processed/YYYY-MM-DD/)
           └── SQLite Event Logging (sightline.db)
                      │
         ┌────────────┴───────────────────────────┐
         ▼                                        ▼
[ Multi-Channel Notifications ]          [ Caddy Reverse Proxy ]
  • Native Web Push (PWA, VAPID)           • Internal TLS (LAN)
  • Apprise (ntfy, Pushover, Discord)      • Let's Encrypt / ACME
  • Firebase Cloud Messaging (FCM)         • Stealth 404 security rules
                                                  │
                                 ┌────────────────┴────────────────┐
                                 ▼                                 ▼
                      [ Local Web Dashboard ]           [ Cloudflare Tunnel ]
                      (https://<nas-ip>:4210)           (Optional Remote Access)
                      (http://<nas-ip>:4280)            (https://sightline.yourdomain.net)
```

---

## Quick Start (Setup from Scratch)

Follow these steps to get Sightline running on your Synology NAS or Linux server in under 5 minutes.

### 1. Clone the Repository

```bash
git clone https://github.com/micatlkw/sightline.git
cd sightline/core
```

### 2. Identify Your User & Group ID (PUID / PGID)

Sightline runs as an unprivileged user inside Docker to prevent permission conflicts with your host files:

```bash
id -u   # Example: 1026 (Synology) or 1000 (Linux)
id -g   # Example: 100  (Synology users group) or 1000 (Linux)
```

### 3. Create Storage Directories

Create persistent host directories and grant ownership to your user ID:

**On Synology NAS (`/volume1/sightline`):**
```bash
sudo mkdir -p /volume1/sightline/{incoming,data,models,config,caddy_data}
sudo chown -R $(id -u):$(id -g) /volume1/sightline
```

**On Generic Linux (`./sightline-storage` or `/opt/sightline`):**
```bash
mkdir -p ./sightline-storage/{incoming,data,models,config,caddy_data}
```
*(If using a custom path like `./sightline-storage`, update the left side of the volume bindings in [`core/docker-compose.yml`](./core/docker-compose.yml)).*

### 4. Configure Environment Variables (`.env`)

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Open `.env` and verify your `PUID` and `PGID`:
```ini
PUID=1026   # Output from 'id -u'
PGID=100    # Output from 'id -g'
```
*(All other settings have sensible defaults. Leave `TUNNEL_TOKEN` blank if you are not using Cloudflare Tunnel yet).*

### 5. Launch Sightline

```bash
docker compose up -d --build
```

Check the startup logs:
```bash
docker compose logs -f sightline-core
```
On first boot, Sightline will:
1. Initialize the SQLite database and run schema migrations.
2. Auto-download the default YOLO model (`yolo11n.pt`) with SHA-256 verification.
3. Start the `inotify` watcher on `/data/incoming`.
4. Launch Caddy reverse proxy with internal TLS.

### 6. Open the Web Dashboard

Open your web browser and navigate to:
- **HTTPS**: `https://<your-host-ip>:4210` *(Accept the internal self-signed TLS certificate warning)*.
- **HTTP**: `http://<your-host-ip>:4280` *(Direct local LAN access without certificate warnings)*.

---

## Verifying Detection with a Test Clip

Drop any short `.mp4` video clip into your incoming directory to test the pipeline:

```bash
cp test.mp4 /volume1/sightline/incoming/FRONTDOOR_00000001_20260912_120000.mp4
```

Watch the container logs:
```bash
docker compose logs -f sightline-core
```
You will see:
- Inotify detects the new file and waits for file size stability.
- YOLO runs keyframe inference and identifies detected objects (e.g. `person`, `car`, `dog`).
- An animated keyframe GIF thumbnail is saved to `/data/thumbnails/`.
- The event is saved to SQLite, and the video clip is archived to `/data/processed/YYYY-MM-DD/FRONTDOOR/`.
- The Web Dashboard updates live via WebSocket with the new event card!

---

## How Ingestion Works (Feeding Video Clips)

Sightline monitors the `incoming/` directory for `.mp4` files. Any camera, bridge, or service can drop files here:

1. **Filename Convention**:
   Sightline parses the camera identifier from the filename prefix before the first underscore:
   - `<CAMERA_SERIAL>_<SEQUENCE>_<DATE>_<TIME>.mp4` (e.g. `CAM0100000001_00000001_20260912_120000.mp4`)
   - Alternatively, place files inside camera subdirectories: `incoming/Backyard/clip1.mp4`.
2. **Ingestion Sources**:
   - **Arlo SmartHubs**: Use the [`bridge/`](./bridge) daemon to sync clips directly from Arlo USB storage over SMB/CIFS.
   - **IP Cameras / NVRs**: Configure your cameras to upload motion clips to `/volume1/sightline/incoming` via FTP or SMB.
   - **Network Mounts (NFS/SMB)**: If the incoming folder is a remote network mount, set `WATCH_USE_POLLING=true` in `.env` to enable polling instead of inotify.
   - **Manual / Automation API**: Trigger processing directly via REST API:
     ```bash
     curl -X POST http://127.0.0.1:8000/api/v1/clips/process \
       -H "Content-Type: application/json" \
       -d '{"path": "/data/incoming/sample.mp4"}'
     ```

---

## Web Dashboard & Progressive Web App (PWA)

Sightline includes a built-in, responsive web application served directly from the container:

- **Live WebSocket Event Stream**: Real-time event cards appear instantly as motion is detected without refreshing the page.
- **Seekable Video Player**: Built-in HTML5 modal supporting HTTP 206 Range streaming for smooth video scrubbing.
- **Filtering**: Multi-filter events by date, camera name, and detected object class.
- **Settings Editor**: Configure camera names, detection thresholds, sampling rate (`sample_fps`), and edit raw `settings.yaml` directly from the browser with live hot-reloading.
- **Install as an App (PWA)**:
  - **iPhone / iPad (iOS Safari)**: Tap the Share button → **Add to Home Screen**.
  - **Android / Desktop (Chrome / Edge)**: Tap the install icon in the address bar or browser menu → **Install Sightline**.

---

## Notification Setup

Sightline supports three simultaneous notification methods:

### 1. Native Web Push (PWA) — Recommended
Receive instant push notifications on iPhone (iOS 16.4+), Android, and desktop browsers with **zero cloud accounts**:
1. Open the Web Dashboard (`https://<your-host>:4210` or your public domain).
2. Click the **Notification Bell** icon in the dashboard header.
3. Allow browser notifications when prompted.
4. Click **Send Test Push** to confirm.
5. Tapping any alert opens the dashboard and jumps directly to the recorded video clip.

### 2. Apprise (Multi-Service Push)
Send alerts to 80+ push services (ntfy, Pushover, Telegram, Discord, Slack, etc.). Configure `apprise_urls` in `settings.yaml`:
```yaml
apprise_urls:
  # ntfy (free, self-hosted or ntfy.sh):
  - "ntfys://ntfy.sh/my-secret-topic?priority=high&tags=rotating_light,camera"
  # Pushover:
  - "pover://UserKey@AppToken/"
  # Discord Webhook:
  - "discord://webhook_id/webhook_token"
  # Telegram Bot:
  - "tgram://bot_token/chat_id"
```

### 3. Firebase Cloud Messaging (FCM)
For companion mobile applications:
1. Download `serviceAccountKey.json` from your Firebase Console.
2. Rename and save it to `/volume1/sightline/models/firebase_credentials.json`.
3. Set `FIREBASE_CREDENTIALS_PATH=/models/firebase_credentials.json` in `.env`.

---

## Secure Remote Access (Cloudflare Tunnel)

Access Sightline remotely from anywhere without opening ports (80/443) or configuring router port forwarding:

1. In the [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/), navigate to **Networks** → **Tunnels** → **Add a Tunnel**.
2. Name your tunnel (e.g. `sightline`) and select **Cloudflared (Docker)** connector.
3. Copy the tunnel token (`eyJh...`).
4. Paste the token into `core/.env`:
   ```ini
   TUNNEL_TOKEN=eyJh...
   ```
5. In the Cloudflare Tunnel setup screen under **Public Hostnames**:
   - **Subdomain**: `sightline`
   - **Domain**: `yourdomain.com`
   - **Service Type**: `HTTP`
   - **URL**: `caddy:4280` *(or `sightline-caddy:4280`)*
6. Restart the Docker Compose stack:
   ```bash
   docker compose up -d
   ```
7. Your system is now securely accessible worldwide at `https://sightline.yourdomain.com` with automatic HTTPS!

---

## Authentication & Google SSO

To secure your installation when exposed remotely:

1. Configure allowed Google account emails in `settings.yaml` (or via the Web UI Settings tab):
   ```yaml
   allowed_google_emails:
     - "your-email@gmail.com"
     - "family-member@gmail.com"

   admin_google_emails:
     - "your-email@gmail.com"

   google_client_id: "your-google-oauth-client-id.apps.googleusercontent.com"
   ```
2. **Local LAN Bypass**: Trusted local private network requests (192.168.x.x, 10.x.x.x, 172.16-31.x.x, 127.0.0.1) can view the dashboard without Google sign-in when `allow_lan_auth_bypass: true` (default). Remote requests through your public domain strictly require Google sign-in.

---

## Hardware Arlo Bridge (`bridge/`)

If you have proprietary **Arlo SmartHubs or Base Stations** (VMB4000, VMB4500, VMB4540, VMB5000) that only record to local USB drives:

The [`bridge/`](./bridge) subsystem emulates a high-speed USB flash drive using an **Orange Pi Zero 2W** (bus-powered directly by the Arlo hub via USB). It uses a zero-mount userland FAT32 reader to extract recordings in RAM and stream them across SMB to your NAS watch folder with sub-second latency.

See the [**Sightline Bridge Guide**](file:///volume1/sightline/github/sightline/bridge/README.md) for full hardware selection, wiring diagrams, and systemd service setup.

---

## Detailed Documentation & References

- [**Core Setup & Full Configuration Guide**](file:///volume1/sightline/github/sightline/core/README.md): Detailed `.env` and `settings.yaml` options, detection tuning, sample FPS calculation, and API endpoints.
- [**Hardware Arlo Bridge Guide**](file:///volume1/sightline/github/sightline/bridge/README.md): Single-cable USB emulation, zero-mount concurrency solution, and latency benchmarks.

