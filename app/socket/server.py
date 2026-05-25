from __future__ import annotations

import socketio

from app.core.config import get_settings
from app.services.realtime_runtime_service import append_startup_diagnostic, mark_realtime_state
from app.socket.events import register_socket_events

settings = get_settings()

socket_cors_origins = list(settings.cors_origins)
for origin in ("https://useqring.online", "https://localhost", "capacitor://localhost", "ionic://localhost"):
    if origin not in socket_cors_origins:
        socket_cors_origins.append(origin)

sio_manager = None
if settings.redis_enabled:
    sio_manager = socketio.AsyncRedisManager(
        settings.REDIS_URL,
        channel=settings.SOCKET_REDIS_CHANNEL,
        write_only=False,
    )
    append_startup_diagnostic("Socket.IO Redis adapter enabled.")
else:
    append_startup_diagnostic("Socket.IO Redis adapter disabled; using in-memory socket coordination.")

sio = socketio.AsyncServer(
    async_mode="asgi",
    client_manager=sio_manager,
    cors_allowed_origins=socket_cors_origins,
    logger=False,
    engineio_logger=False,
    ping_interval=20,
    ping_timeout=30,
)

mark_realtime_state(
    websocketInitialized=True,
    redisConnected=bool(settings.redis_enabled),
    redisError="",
    socketNamespaces=[settings.DASHBOARD_NAMESPACE, settings.SIGNALING_NAMESPACE],
    socketPath=settings.SOCKET_PATH,
    socketServerMounted=False,
    socketRedisAdapterAttached=bool(sio_manager),
)
append_startup_diagnostic(
    f"Socket.IO initialized with path {settings.SOCKET_PATH} and namespaces "
    f"{settings.DASHBOARD_NAMESPACE}, {settings.SIGNALING_NAMESPACE}."
)

register_socket_events(sio)
