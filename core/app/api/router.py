from fastapi import APIRouter

from app.api.routes import auth, clips, devices, events, health, notifications, preferences, settings, ws


def build_router() -> APIRouter:
    router = APIRouter()
    router.include_router(auth.router)
    router.include_router(health.router)
    router.include_router(events.router)
    router.include_router(clips.router)
    router.include_router(devices.router)
    router.include_router(preferences.router)
    router.include_router(settings.router)
    router.include_router(notifications.router)
    router.include_router(ws.router)
    return router

