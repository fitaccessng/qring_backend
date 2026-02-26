import socketio

from app.core.config import get_settings
from app.socket.events import register_socket_events

settings = get_settings()

socket_cors_origins = list(settings.cors_origins)
for origin in ("http://localhost", "https://localhost", "capacitor://localhost", "ionic://localhost"):
    if origin not in socket_cors_origins:
        socket_cors_origins.append(origin)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*" if settings.DEBUG else socket_cors_origins,
    logger=False,
    engineio_logger=False,
)

register_socket_events(sio)
