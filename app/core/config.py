from functools import lru_cache
from pathlib import Path
from typing import List
from urllib.parse import urlparse
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), case_sensitive=False)

    APP_NAME: str = "Qring Backend"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000

    DATABASE_URL: str = "sqlite:///./qring.db"

    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    CORS_ORIGINS: str = (
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "https://useqring.online,"
        "https://www.useqring.online"
    )
    # Dev-friendly default: allow localhost + RFC1918 private LAN ranges so phones/tablets on the same Wi-Fi can reach the API.
    # In production, set CORS_ORIGINS / CORS_ALLOW_ORIGIN_REGEX explicitly to your real domain(s).
    CORS_ALLOW_ORIGIN_REGEX: str = (
        r"^https?://("
        r"localhost|127\\.0\\.0\\.1|"
        r"192\\.168\\.\\d{1,3}\\.\\d{1,3}|"
        r"10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}|"
        r"172\\.(1[6-9]|2\\d|3[0-1])\\.\\d{1,3}\\.\\d{1,3}|"
        r"useqring\\.online|www\\.useqring\\.online"
        r")(\\:\\d+)?$"
    )

    SOCKET_PATH: str = "/socket.io"
    DASHBOARD_NAMESPACE: str = "/realtime/dashboard"
    SIGNALING_NAMESPACE: str = "/realtime/signaling"

    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:admin@useqring.online"
    PAYSTACK_SECRET_KEY: str = ""
    PAYSTACK_PUBLIC_KEY: str = ""
    FRONTEND_BASE_URL: str = "http://localhost:5173"
    FIREBASE_PROJECT_ID: str = ""
    ADMIN_SIGNUP_KEY: str = ""

    @property
    def cors_origins(self) -> List[str]:
        origins: list[str] = []
        for raw in self.CORS_ORIGINS.split(","):
            value = raw.strip()
            if not value:
                continue
            parsed = urlparse(value)
            if parsed.scheme and parsed.netloc:
                value = f"{parsed.scheme}://{parsed.netloc}"
            origins.append(value.rstrip("/"))
        return origins


@lru_cache
def get_settings() -> Settings:
    return Settings()

