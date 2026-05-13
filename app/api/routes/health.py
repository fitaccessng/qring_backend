from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter()
settings = get_settings()


def _livekit_configured() -> bool:
    return bool(
        (settings.LIVEKIT_URL or "").strip()
        and (settings.LIVEKIT_API_KEY or "").strip()
        and (settings.LIVEKIT_API_SECRET or "").strip()
    )


@router.get("/health")
def health():
    return {
        "status": "ok",
        "livekitConfigured": _livekit_configured(),
        "environment": settings.ENVIRONMENT,
        "databaseBackend": settings.database_backend,
        "redisEnabled": settings.redis_enabled,
        "processRole": settings.PROCESS_ROLE,
    }
