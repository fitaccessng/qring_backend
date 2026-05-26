from __future__ import annotations

from typing import Any

from app.core.config import Settings

_DEFAULT_ALLOWED_ORIGINS = (
    "https://useqring.online",
    "https://www.useqring.online",
    "http://localhost:5173",
    "http://localhost:3000",
)


def get_allowed_origins(settings: Settings) -> list[str]:
    origins: list[str] = []
    for origin in [*settings.cors_origins, *_DEFAULT_ALLOWED_ORIGINS]:
        value = str(origin or "").rstrip("/")
        if value and value not in origins:
            origins.append(value)
    return origins


def get_cors_settings(settings: Settings) -> dict[str, Any]:
    return {
        "allow_origins": get_allowed_origins(settings),
        "allow_origin_regex": settings.cors_allow_origin_regex,
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


def is_allowed_origin(settings: Settings, origin: str | None) -> bool:
    normalized = str(origin or "").rstrip("/")
    if not normalized:
        return False
    return normalized in get_allowed_origins(settings)
