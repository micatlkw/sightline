from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.auth.google_sso import User, check_public_rate_limit, get_current_user
from app.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"])

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.is_dir():
    router.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_CACHED_INDEX_CONTENT: str | None = None
_CACHED_INDEX_MTIME: float = 0.0


def get_cached_dashboard_html() -> str:
    """Returns cached index.html, automatically reloading if file mtime changed on disk."""
    global _CACHED_INDEX_CONTENT, _CACHED_INDEX_MTIME
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="Web UI template not found")

    try:
        current_mtime = index_file.stat().st_mtime
    except Exception:
        current_mtime = 0.0

    if _CACHED_INDEX_CONTENT is None or current_mtime != _CACHED_INDEX_MTIME:
        _CACHED_INDEX_CONTENT = index_file.read_text(encoding="utf-8")
        _CACHED_INDEX_MTIME = current_mtime
        logger.debug("[ui] reloaded index.html template into memory cache")

    return _CACHED_INDEX_CONTENT


@router.get("/", response_class=HTMLResponse, summary="Serve Web UI Dashboard")
async def get_dashboard(
    request: Request,
) -> HTMLResponse:
    check_public_rate_limit(request)
    content = get_cached_dashboard_html()
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0891b2"/>
      <stop offset="100%" stop-color="#4f46e5"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="16" fill="url(#g)"/>
  <rect x="13" y="20" width="25" height="24" rx="4.5" fill="#ffffff"/>
  <path d="M40.5 27.5L50.2 21.2C51.4 20.4 53 21.3 53 22.8L53 41.2C53 42.7 51.4 43.6 50.2 42.8L40.5 36.5Z" fill="#ffffff"/>
  <circle cx="21" cy="27" r="2.5" fill="#0891b2"/>
</svg>"""


@router.get("/favicon.svg", summary="Serve SVG favicon")
@router.get("/favicon.ico", summary="Serve favicon")
async def get_favicon() -> Response:
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


MANIFEST_JSON = """{
  "name": "Sightline — AI Surveillance",
  "short_name": "Sightline",
  "description": "Local AI Video Surveillance Dashboard",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#090d16",
  "theme_color": "#0f172a",
  "orientation": "any",
  "icons": [
    {
      "src": "/favicon.svg",
      "sizes": "any",
      "type": "image/svg+xml",
      "purpose": "any"
    },
    {
      "src": "/static/icons/icon-192x192.png",
      "sizes": "192x192",
      "type": "image/png",
      "purpose": "any"
    },
    {
      "src": "/favicon.svg",
      "sizes": "192x192 512x512",
      "type": "image/svg+xml",
      "purpose": "maskable"
    }
  ]
}"""


@router.get("/manifest.webmanifest", summary="Serve PWA manifest")
@router.get("/manifest.json", summary="Serve PWA manifest JSON")
async def get_manifest() -> Response:
    return Response(
        content=MANIFEST_JSON,
        media_type="application/manifest+json",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        },
    )


SERVICE_WORKER_JS = """// Sightline PWA Service Worker v8
const CACHE_NAME = 'sightline-v8';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            console.log('[SW] Purging old cache:', key);
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Allow browser native network handling for navigation, APIs, WebSockets, and mutations.
// Returning directly without event.respondWith() lets the browser execute reloads natively
// without Service Worker fetch pipeline failures (eliminates Chromium ERR_FAILED on reload).
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept cross-origin requests (e.g. Google avatars, external CDNs).
  // Cross-origin requests must be handled natively by the browser according to
  // their respective CSP rules (e.g. img-src for avatars).
  if (url.origin !== self.location.origin) {
    return;
  }

  if (
    event.request.mode === 'navigate' ||
    url.pathname === '/' ||
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/ws') ||
    event.request.method !== 'GET'
  ) {
    return;
  }

  // Fallback network-first for local static resources with cache fallback
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

// ── Native Web Push Event Listener ──────────────────────────────────────────
self.addEventListener('push', (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { title: 'Sightline Alert', body: event.data.text() };
    }
  }

  const title = data.title || 'Sightline Alert';
  const options = {
    body: data.body || 'Camera activity detected',
    icon: data.icon || '/static/icons/icon-192x192.png',
    badge: data.badge || '/static/icons/badge-72x72.png',
    image: data.image || undefined,
    data: data.data || {},
    vibrate: [200, 100, 200],
    requireInteraction: true,
    tag: `sightline-${data.data?.event_id || Date.now()}`,
    renotify: true,
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

// ── Notification Click Action (Tap-to-Play Deep Link) ────────────────────────
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const rawUrl = event.notification.data?.url;
  // Strictly validate local relative URL to prevent open redirect or scheme injection
  const targetPath = (typeof rawUrl === 'string' && rawUrl.startsWith('/') && !rawUrl.startsWith('//'))
    ? rawUrl
    : '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) {
          if ('navigate' in client) {
            client.navigate(targetPath);
          }
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetPath);
      }
    })
  );
});
"""


@router.get("/sw.js", summary="Serve PWA Service Worker")
async def get_service_worker() -> Response:
    return Response(
        content=SERVICE_WORKER_JS,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/healthz", summary="Web UI Health Check")
async def health_check(current_user: User = Depends(get_current_user)) -> dict[str, str]:
    return {"status": "healthy", "service": "sightline-web-ui"}
