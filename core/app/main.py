"""
sightline-core — FastAPI application factory.

Startup order:
  1. Database connects + migrations run
  2. YOLO model loads (may download on first run)
  3. Notifiers initialise (Apprise validates URLs)
  4. Pipeline starts (watcher begins, processing loop runs)

Shutdown order (reverse):
  1. Pipeline stops (watcher unscheduled, processing loop cancelled)
  2. Notifiers shut down
  3. Database connection closed
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
from typing import AsyncIterator
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth.google_sso import get_client_ip, is_allowed_ws_origin
from app.config import Settings, load_effective_settings
from app.core.event_bus import EventBus
from app.core.pipeline import Pipeline
from app.core.settings_watcher import SettingsWatcher
from app.database import Database
from app.detector.yolo_detector import YoloDetector
from app.notifier.apprise_notifier import AppriseNotifier
from app.notifier.composite import CompositeNotifier
from app.notifier.firebase_notifier import FirebaseNotifier
from app.notifier.webpush_notifier import WebPushNotifier
from app.watcher.directory_watcher import DirectoryWatcher
from app.api.router import build_router
from app.ui.router import router as ui_router

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── App factory ───────────────────────────────────────────────────────────────


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    cfg = settings or load_effective_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ── Startup ───────────────────────────────────────────────────────────
        logger.info("sightline-core starting up…")

        # Auto-create settings.yaml if missing so user can immediately edit it
        if not cfg.settings_config_path.exists():
            cfg.save_to_yaml()

        # Auto-create Caddyfile for Caddy reverse proxy
        cfg.save_caddyfile()

        # Ensure required directories exist if permissions allow
        for d in (cfg.incoming_dir, cfg.processed_dir, cfg.thumbnails_dir, cfg.models_dir, cfg.db_path.parent):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning(f"Could not create directory {d}: {e}")

        db = Database(cfg)
        await db.connect()

        # Load active user session revocations into memory cache
        try:
            app.state.user_revocations = await db.get_user_session_revocations()
            logger.info(f"[auth] loaded {len(app.state.user_revocations)} active user session revocation epoch(s)")
        except Exception as exc:
            logger.warning(f"[auth] could not load user session revocations: {exc}")
            app.state.user_revocations = {}

        # Prune expired session token revocations and missing events on startup
        try:
            pruned = await db.prune_expired_revocations()
            pruned_users = await db.prune_expired_user_revocations()
            if pruned > 0 or pruned_users > 0:
                logger.info(f"[database] pruned {pruned} expired token(s), {pruned_users} user revocation(s) on startup")
            startup_missing = await db.prune_missing_events(days=7, settings=cfg)
            if startup_missing:
                logger.info(f"[database] startup sync pruned {len(startup_missing)} missing event(s)")
        except Exception as exc:
            logger.warning(f"[database] startup database pruning error: {exc}")

        # Schedule periodic daily token revocation and database integrity pruning
        async def _periodic_prune_task() -> None:
            while True:
                try:
                    await asyncio.sleep(86400)
                    p = await db.prune_expired_revocations()
                    pu = await db.prune_expired_user_revocations()
                    if p > 0 or pu > 0:
                        logger.info(f"[database] pruned {p} expired token(s), {pu} user revocation(s)")
                    pruned_missing = await db.prune_missing_events(full=True, settings=cfg)
                    if pruned_missing:
                        logger.info(f"[database] periodic maintenance pruned {len(pruned_missing)} missing event(s)")
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.warning(f"[database] periodic pruning error: {exc}")

        prune_task = asyncio.create_task(_periodic_prune_task())

        bus = EventBus()

        # The clip queue is shared between the watcher (producer) and the
        # pipeline (consumer).  Must be created inside the event loop.
        clip_queue: asyncio.Queue = asyncio.Queue()

        watcher = DirectoryWatcher(cfg, clip_queue)

        # Build notifier stack
        notifiers = []
        if cfg.firebase_credentials_path:
            notifiers.append(FirebaseNotifier(cfg.firebase_credentials_path, db))
        if cfg.apprise_urls:
            notifiers.append(AppriseNotifier(cfg.apprise_urls))

        # Native Web Push Notifier (PWA)
        webpush_notifier = WebPushNotifier(db=db, settings=cfg)
        notifiers.append(webpush_notifier)
        app.state.webpush_notifier = webpush_notifier

        notifier = CompositeNotifier(notifiers, cfg.alert_cooldown_seconds)

        # YOLO model load (may take a few seconds)
        detector = YoloDetector(cfg)

        pipeline = Pipeline(
            settings=cfg,
            db=db,
            detector=detector,
            watcher=watcher,
            notifier=notifier,
            bus=bus,
            clip_queue=clip_queue,
        )

        def on_settings_reloaded(new_settings: Settings) -> None:
            app.state.settings = new_settings
            if hasattr(app.state, "pipeline") and app.state.pipeline:
                app.state.pipeline.update_settings(new_settings)
            new_settings.save_caddyfile()
            logger.info(f"[main] hot-applied settings from {new_settings.settings_config_path}")

        settings_watcher = SettingsWatcher(
            config_path=cfg.settings_config_path,
            on_reloaded=on_settings_reloaded,
        )

        await notifier.startup()
        await pipeline.start()
        await settings_watcher.start()

        # Expose shared state to route handlers via app.state
        app.state.settings = cfg
        app.state.db = db
        app.state.bus = bus
        app.state.pipeline = pipeline
        app.state.watcher = watcher
        app.state.notifier = notifier
        app.state.settings_watcher = settings_watcher

        # Launch dedicated Web UI server on ui_port (default 8080) if configured
        ui_server_task = None
        ui_server = None
        if cfg.ui_port and cfg.ui_port != cfg.api_port:
            ui_config = uvicorn.Config(
                app=app,
                host=cfg.ui_host,
                port=cfg.ui_port,
                lifespan="off",
                log_level="info",
            )
            ui_server = uvicorn.Server(ui_config)
            # Prevent secondary server from conflicting with main event loop signal handlers
            ui_server.install_signal_handlers = lambda: None

            async def _run_ui_server() -> None:
                try:
                    await ui_server.serve()
                except Exception as exc:
                    logger.error(f"[main] Web UI server encountered an error: {exc}", exc_info=True)

            ui_server_task = asyncio.create_task(_run_ui_server())
            logger.info(f"[main] Web UI Dashboard listening on http://{cfg.ui_host}:{cfg.ui_port} ✓")

        logger.info(f"sightline-core API ready on http://{cfg.api_host}:{cfg.api_port} ✓")
        if cfg.https_port:
            logger.info(f"[main] Reverse proxy configured on https://{cfg.domain_name or '0.0.0.0'}:{cfg.https_port} (HTTP redirect on :{cfg.http_port}) ✓")

        yield

        # ── Shutdown ──────────────────────────────────────────────────────────
        logger.info("sightline-core shutting down…")
        if ui_server and ui_server_task:
            ui_server.should_exit = True
            try:
                await asyncio.wait_for(ui_server_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if prune_task:
            prune_task.cancel()
        await settings_watcher.stop()
        await pipeline.stop()
        await notifier.shutdown()
        await db.close()
        logger.info("sightline-core stopped")

    app = FastAPI(
        title="sightline-core",
        description=(
            "Event-driven surveillance engine. "
            "Monitors a directory for .mp4 clips, runs YOLO detection, "
            "and exposes REST + WebSocket APIs."
        ),
        version="0.1.0",
        docs_url="/docs" if cfg.enable_api_docs else None,
        redoc_url="/redoc" if cfg.enable_api_docs else None,
        openapi_url="/openapi.json" if cfg.enable_api_docs else None,
        lifespan=lifespan,
    )
    app.state.user_revocations = {}

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            raise exc
        error_id = secrets.token_hex(6)
        client_ip = get_client_ip(request, getattr(request.app.state, "settings", cfg))
        logger.error(
            f"[AUDIT] [INTERNAL_ERROR] [ref={error_id}] {request.method} {request.url.path} from {client_ip}: {exc}",
            exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "An unexpected server error occurred. Please contact your administrator.",
                "error_id": error_id,
            },
        )

    @app.middleware("http")
    async def csrf_origin_validation_middleware(request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if not origin:
                referer = request.headers.get("referer")
                if referer:
                    try:
                        p = urlparse(referer)
                        origin = f"{p.scheme}://{p.netloc}"
                    except Exception:
                        origin = None

            if origin:
                from app.auth.google_sso import is_allowed_ws_origin
                app_cfg = getattr(request.app.state, "settings", cfg)
                if not is_allowed_ws_origin(origin, app_cfg):
                    client_ip = get_client_ip(request, app_cfg)
                    logger.warning(
                        f"[AUDIT] [CSRF_REJECTED] Rejected cross-origin {request.method} request to {request.url.path} from untrusted origin: {origin} (ip: {client_ip})"
                    )
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={"detail": "Cross-origin requests are forbidden from this origin"},
                    )
        return await call_next(request)

    # Restrict CORS to configured domain, standard local/LAN origins, and mobile webviews
    cors_origins = [
        "capacitor://localhost",
        "http://localhost",
        "https://localhost",
    ]
    if cfg.domain_name and cfg.domain_name.strip():
        dom = cfg.domain_name.strip()
        cors_origins.append(f"https://{dom}")
        if cfg.https_port and cfg.https_port != 443:
            cors_origins.append(f"https://{dom}:{cfg.https_port}")

    # Allow local LAN / private IP origins (192.168.x.x, 10.x.x.x, 172.16-31.x.x, 127.0.0.1, localhost) on any port
    lan_origin_regex = r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?$"

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=lan_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    try:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        # Only trust loopback and Docker container network gateways as reverse proxies
        app.add_middleware(
            ProxyHeadersMiddleware,
            trusted_hosts=["127.0.0.1", "::1", "172.16.0.0/12", "172.17.0.0/16", "172.18.0.0/16", "172.19.0.0/16", "172.20.0.0/16", "testclient"],
        )
    except Exception as exc:
        logger.debug(f"[main] ProxyHeadersMiddleware could not be added: {exc}")

    # Mount static assets
    static_dir = Path(__file__).parent / "ui" / "static"
    if static_dir.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Mount API routers at both /api/v1 and root for seamless compatibility
    api_router = build_router()
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(api_router)
    app.include_router(ui_router)
    return app


# ── Module-level app instance (used by uvicorn) ───────────────────────────────

app = create_app()


# ── Direct execution ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    s = Settings()
    uvicorn.run(
        "app.main:app",
        host=s.api_host,
        port=s.api_port,
        log_level="info",
        reload=False,
    )
