from __future__ import annotations

import socketio

from app.core.config import get_settings
from app.socket.events import register_socket_events

settings = get_settings()

socket_cors_origins = list(settings.cors_origins)
for origin in ("http://localhost", "https://localhost", "capacitor://localhost", "ionic://localhost"):
    if origin not in socket_cors_origins:
        socket_cors_origins.append(origin)

sio_manager = None
if settings.redis_enabled:
    sio_manager = socketio.AsyncRedisManager(
        settings.REDIS_URL,
        channel=settings.SOCKET_REDIS_CHANNEL,
        write_only=False,
    )

sio = socketio.AsyncServer(
    async_mode="asgi",
    client_manager=sio_manager,
    cors_allowed_origins=socket_cors_origins,
    logger=False,
    engineio_logger=False,
    ping_interval=20,
    ping_timeout=30,
)

register_socket_events(sio)
