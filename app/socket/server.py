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

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*" if allow_all_socket_cors else socket_cors_origins,
    logger=False,
    engineio_logger=False,
)

register_socket_events(sio)
