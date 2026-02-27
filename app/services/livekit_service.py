from app.core.config import get_settings
from app.core.exceptions import AppException

settings = get_settings()


def ensure_livekit_configured() -> None:
    if not settings.LIVEKIT_URL or not settings.LIVEKIT_API_KEY or not settings.LIVEKIT_API_SECRET:
        raise AppException("LiveKit is not configured on the server.", status_code=503)


def build_room_name(session_id: str) -> str:
    safe_session = str(session_id or "").strip()
    if not safe_session:
        raise AppException("Invalid session id", status_code=400)
    return f"{settings.LIVEKIT_ROOM_PREFIX}{safe_session}"


def issue_livekit_token(
    *,
    session_id: str,
    identity: str,
    display_name: str,
    can_publish: bool = True,
    can_subscribe: bool = True,
) -> dict:
    ensure_livekit_configured()

    try:
        from livekit import api as livekit_api
    except Exception as exc:  # pragma: no cover - import guard for misconfigured installs
        raise AppException(f"LiveKit SDK import failed: {exc}", status_code=500)

    room_name = build_room_name(session_id)
    grants = livekit_api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
    )
    token = (
        livekit_api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(display_name or identity)
        .with_grants(grants)
        .to_jwt()
    )
    return {
        "url": settings.LIVEKIT_URL,
        "roomName": room_name,
        "token": token,
    }
