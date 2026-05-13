from __future__ import annotations

import socketio

from app.core.config import get_settings
from app.socket.events import register_socket_events

settings = get_settings()

socket_cors_origins = list(settings.cors_origins)
for origin in ("http://localhost", "https://localhost", "capacitor://localhost", "ionic://localhost"):
    if origin not in socket_cors_origins:
        socket_cors_origins.append(origin)

allow_all_socket_cors = settings.DEBUG or settings.ENVIRONMENT.lower().strip() == "development"
raw_cors_origins = (settings.CORS_ORIGINS or "").strip()
if raw_cors_origins == "*" or settings.cors_allow_origin_regex:
    allow_all_socket_cors = True

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
    cors_allowed_origins="*" if allow_all_socket_cors else socket_cors_origins,
    logger=False,
    engineio_logger=False,
)

register_socket_events(sio)
