import socketio

from app.core.config import get_settings
from app.socket.events import register_socket_events

settings = get_settings()

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*" if settings.DEBUG else settings.cors_origins,
    logger=False,
    engineio_logger=False,
)

register_socket_events(sio)
