from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse
from pydantic import field_validator
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ModuleNotFoundError:  # pragma: no cover - local test fallback
    class BaseSettings:
        def __init__(self, **kwargs):
            env_values = _load_env_files(ENV_FILES)
            annotations = getattr(self.__class__, "__annotations__", {})
            for field_name in annotations:
                if field_name.startswith("_"):
                    continue
                default = getattr(self.__class__, field_name, None)
                raw_value = kwargs.get(field_name, os.getenv(field_name, env_values.get(field_name, default)))
                setattr(self, field_name, _coerce_value(default, raw_value))

    def SettingsConfigDict(**kwargs):
        return kwargs

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MONOREPO_ROOT = BACKEND_ROOT.parent


def _resolve_env_files() -> list[str]:
    explicit = (os.getenv("QRING_ENV_FILE") or os.getenv("ENV_FILE") or "").strip()
    if explicit:
        return [explicit]

    runtime_env = (os.getenv("ENVIRONMENT") or "").strip().lower()
    if runtime_env in {"production", "staging"}:
        candidates = [
            MONOREPO_ROOT / ".env.production",
            BACKEND_ROOT / ".env.production",
            MONOREPO_ROOT / ".env",
            BACKEND_ROOT / ".env",
        ]
    else:
        candidates = [
            MONOREPO_ROOT / ".env",
            BACKEND_ROOT / ".env",
        ]
    return [str(path) for path in candidates if path.exists()]


ENV_FILES = _resolve_env_files()


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _load_env_files(paths: list[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for raw_path in paths:
        try:
            merged.update(_load_env_file(Path(raw_path)))
        except Exception:
            continue
    return merged


def _coerce_value(default, value):
    if value is None:
        return default
    if isinstance(default, bool):
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except Exception:
            return default
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES or None, case_sensitive=False, extra="ignore")

    APP_NAME: str = "Qring Backend"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000

    DATABASE_URL: str = "sqlite:///./qring.db"

    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 20
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # CORS_ORIGINS must include all local dev and production domains for frontend access
    CORS_ORIGINS: str = (
        "http://localhost:5173,"  # Vite/React dev server
        "http://localhost:5174," 
        "http://localhost:5175," 
        "http://127.0.0.1:5173,"  # Vite/React dev server (IP)
        "http://127.0.0.1:5174," 
        "http://127.0.0.1:5175," 
        "capacitor://localhost," 
        "ionic://localhost," 
        "https://qring.io," 
        "https://www.qring.io," 
        "https://useqring.online," 
        "https://www.useqring.online"
    )
    # Dev-friendly default: allow localhost + RFC1918 private LAN ranges so phones/tablets on the same Wi-Fi can reach the API.
    # In production, set CORS_ORIGINS / CORS_ALLOW_ORIGIN_REGEX explicitly to your real domain(s).
    CORS_ALLOW_ORIGIN_REGEX: str = (
        r"^(https?|capacitor|ionic)://("
        r"localhost|127\\.0\\.0\\.1|"
        r"192\\.168\\.\\d{1,3}\\.\\d{1,3}|"
        r"10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}|"
        r"172\\.(1[6-9]|2\\d|3[0-1])\\.\\d{1,3}\\.\\d{1,3}|"
        r"qring\\.io|www\\.qring\\.io|"
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
    FRONTEND_BASE_URL: str = "https://www.useqring.online"
    FIREBASE_PROJECT_ID: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""
    FIREBASE_SERVICE_ACCOUNT_BASE64: str = ""
    FIREBASE_STORAGE_BUCKET: str = ""
    ADMIN_SIGNUP_KEY: str = ""
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""
    LIVEKIT_WEBHOOK_SECRET: str = ""
    LIVEKIT_ROOM_PREFIX: str = "qring-session-"
    APPOINTMENT_SHARE_BASE_URL: str = "https://www.useqring.online"
    MEDIA_STORAGE_PATH: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@useqring.online"
    SMS_PROVIDER_API_KEY: str = ""
    SMS_PROVIDER_BASE_URL: str = ""
    SMS_PROVIDER_SENDER_ID: str = "Qring"
    FACE_RECOGNITION_API_URL: str = ""
    FACE_RECOGNITION_API_KEY: str = ""
    QR_TOKEN_SIGNING_KEY: str = ""
    QR_TOKEN_ENCRYPTION_KEY: str = ""
    APPOINTMENT_DEFAULT_GEOFENCE_RADIUS_METERS: int = 120
    RATE_LIMIT_WINDOW_SECONDS: int = 900
    RATE_LIMIT_MAX_REQUESTS: int = 100
    RATE_LIMIT_AUTH_WINDOW_SECONDS: int = 60
    RATE_LIMIT_AUTH_MAX_REQUESTS: int = 20

    _MANDATORY_CORS_ORIGINS = (
        "https://qring.io",
        "https://www.qring.io",
        "https://useqring.online",
        "https://www.useqring.online",
    )

    @field_validator("DEBUG", mode="before")
    @classmethod
    def _normalize_debug(cls, value):
        if isinstance(value, bool):
            return value
        raw = str(value or "").strip().lower()
        if raw in {"1", "true", "yes", "on", "debug", "development", "dev", "local"}:
            return True
        if raw in {"0", "false", "no", "off", "release", "production", "prod", "staging"}:
            return False
        return value

    @property
    def cors_origins(self) -> List[str]:
        origins: list[str] = []
        for raw in self.CORS_ORIGINS.split(","):
            value = raw.strip()
            if not value:
                continue
            if "://" not in value:
                value = f"https://{value}"
            parsed = urlparse(value)
            if parsed.scheme and parsed.netloc:
                value = f"{parsed.scheme}://{parsed.netloc}"
            origins.append(value.rstrip("/"))
        for required in self._MANDATORY_CORS_ORIGINS:
            canonical = required.rstrip("/")
            if canonical not in origins:
                origins.append(canonical)
        return origins

    @property
    def cors_allow_origin_regex(self) -> Optional[str]:
        value = (self.CORS_ALLOW_ORIGIN_REGEX or "").strip()
        return value or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
