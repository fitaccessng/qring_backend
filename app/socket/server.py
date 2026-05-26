from __future__ import annotations

import socketio

from app.core.redis import describe_redis_configuration
from app.core.config import get_settings
from app.core.cors import get_allowed_origins
from app.services.realtime_runtime_service import append_startup_diagnostic, mark_realtime_state
from app.socket.events import register_socket_events

settings = get_settings()

socket_cors_origins = get_allowed_origins(settings)
for origin in ("https://localhost", "capacitor://localhost", "ionic://localhost"):
    if origin not in socket_cors_origins:
        socket_cors_origins.append(origin)

sio_manager = None
redis_config = describe_redis_configuration()
if settings.redis_enabled:
    sio_manager = socketio.AsyncRedisManager(
        settings.REDIS_URL,
        channel=settings.SOCKET_REDIS_CHANNEL,
        write_only=False,
    )
    append_startup_diagnostic(
        f"Socket.IO Redis adapter configured for host={redis_config['host'] or 'unknown'} channel={settings.SOCKET_REDIS_CHANNEL}.",
        code="socket.redis.configured",
    )
else:
    append_startup_diagnostic(
        "Socket.IO Redis adapter disabled; using in-memory socket coordination.",
        level="warning",
        code="socket.redis.disabled",
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

mark_realtime_state(
    websocketInitialized=True,
    redisConfigured=bool(settings.redis_enabled),
    redisConnected=False,
    redisError="",
    redisUrl=redis_config["url"],
    redisHost=redis_config["host"],
    redisAdapterMode="redis" if sio_manager else "memory",
    socketNamespaces=[settings.DASHBOARD_NAMESPACE, settings.SIGNALING_NAMESPACE],
    socketPath=settings.SOCKET_PATH,
    socketServerMounted=False,
    socketRedisAdapterAttached=bool(sio_manager),
)
append_startup_diagnostic(
    f"Socket.IO initialized with path {settings.SOCKET_PATH} and namespaces "
    f"{settings.DASHBOARD_NAMESPACE}, {settings.SIGNALING_NAMESPACE}.",
    code="socket.initialized",
)

register_socket_events(sio)
