from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.auth.google_sso import get_client_ip, get_current_user_ws, is_allowed_ws_origin
from app.config import Settings
from app.core.event_bus import EventBus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

MAX_CONCURRENT_WS_PER_IP = 30
_ACTIVE_WS_CONNECTIONS: dict[str, int] = {}


async def _handle_ws_stream(websocket: WebSocket, all_topics: bool = True) -> None:
    cfg: Settings = getattr(websocket.app.state, "settings", None) or Settings()

    # Validate Origin header to protect against Cross-Site WebSocket Hijacking (CSWSH)
    origin = websocket.headers.get("origin")
    if origin and not is_allowed_ws_origin(origin, cfg):
        logger.warning(f"[ws] rejected untrusted cross-origin connection from {websocket.client} with Origin: {origin}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Extract client IP for concurrency limit
    client_ip = get_client_ip(websocket, cfg)

    if _ACTIVE_WS_CONNECTIONS.get(client_ip, 0) >= MAX_CONCURRENT_WS_PER_IP:
        logger.warning(f"[ws] rejected connection from {client_ip}: max concurrent limit ({MAX_CONCURRENT_WS_PER_IP}) reached")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    user = await get_current_user_ws(cfg, websocket=websocket)
    if not user:
        logger.warning(f"[ws] rejected unauthenticated connection from {websocket.client}")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Accept subprotocol if requested by client (e.g. sightline.auth token bearer)
    subprotocol = None
    sec_proto = websocket.headers.get("sec-websocket-protocol", "")
    if sec_proto:
        requested_protos = [p.strip() for p in sec_proto.split(",") if p.strip()]
        if "sightline.auth" in requested_protos:
            subprotocol = "sightline.auth"

    bus: EventBus = websocket.app.state.bus
    await websocket.accept(subprotocol=subprotocol)
    _ACTIVE_WS_CONNECTIONS[client_ip] = _ACTIVE_WS_CONNECTIONS.get(client_ip, 0) + 1

    # Subscribed queues
    topics = (
        ["detection", "clip.queued", "clip.done", "clip.error", "event.thumbnail_ready", "event.deleted"]
        if all_topics
        else ["detection"]
    )
    queues = {topic: bus.subscribe(topic) for topic in topics}
    client = websocket.client
    logger.info(f"[ws] client connected: {client} (user: {user.email}, active for ip: {_ACTIVE_WS_CONNECTIONS[client_ip]})")

    async def _reader_task(topic: str, q: asyncio.Queue):
        try:
            while True:
                payload = await q.get()
                msg = {"event": topic, "data": payload} if all_topics else payload
                await websocket.send_json(msg)
        except Exception as e:
            logger.debug(f"[ws] reader task closed on {topic}: {e}")

    tasks = [asyncio.create_task(_reader_task(topic, q)) for topic, q in queues.items()]

    try:
        while True:
            # 45s heartbeat timeout to detect and clean up dropped mobile/sleeping connections
            data = await asyncio.wait_for(websocket.receive_text(), timeout=45.0)
            if data == "ping":
                await websocket.send_text("pong")
    except (asyncio.TimeoutError, TimeoutError):
        logger.debug(f"[ws] client idle timeout (no activity for 45s): {client}")
    except WebSocketDisconnect as exc:
        logger.info(f"[ws] client disconnected: {client} code={exc.code}")
    except Exception as exc:
        logger.debug(f"[ws] connection closed for {client}: {exc}")
    finally:
        current_active = _ACTIVE_WS_CONNECTIONS.get(client_ip, 0)
        if current_active <= 1:
            _ACTIVE_WS_CONNECTIONS.pop(client_ip, None)
        else:
            _ACTIVE_WS_CONNECTIONS[client_ip] = current_active - 1
        for t in tasks:
            t.cancel()
        for q in queues.values():
            bus.unsubscribe(q)


@router.websocket("/ws")
async def ws_general(websocket: WebSocket) -> None:
    """Real-time event and pipeline activity stream."""
    await _handle_ws_stream(websocket, all_topics=True)


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    """Real-time detection event stream."""
    await _handle_ws_stream(websocket, all_topics=True)


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """Real-time detection event stream (live alias)."""
    await _handle_ws_stream(websocket, all_topics=True)
