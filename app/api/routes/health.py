from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings
from app.services.realtime_config_service import webrtc_realtime_configured

router = APIRouter()
settings = get_settings()


@router.get("/health")
def health():
    return {
        "status": "ok",
        "realtimeConfigured": webrtc_realtime_configured(),
        "turnConfigured": webrtc_realtime_configured(),
        "stunUrl": settings.WEBRTC_STUN_URL,
        "environment": settings.ENVIRONMENT,
        "databaseBackend": settings.database_backend,
        "redisEnabled": settings.redis_enabled,
        "processRole": settings.PROCESS_ROLE,
    }
