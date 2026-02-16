from app.core.config import get_settings

settings = get_settings()

host = settings.BACKEND_HOST
port = settings.BACKEND_PORT
log_level = "debug" if settings.DEBUG else "info"
workers = 1 if settings.DEBUG else 2
