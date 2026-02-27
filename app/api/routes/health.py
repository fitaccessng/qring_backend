from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter()
settings = get_settings()


def _livekit_configured() -> bool:
    return bool(
        settings.LIVEKIT_URL.strip()
        and settings.LIVEKIT_API_KEY.strip()
        and settings.LIVEKIT_API_SECRET.strip()
    )


@router.get("/health")
def health():
    return {
        "status": "ok",
        "livekitConfigured": _livekit_configured(),
        "environment": settings.ENVIRONMENT,
    }
