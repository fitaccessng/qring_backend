from __future__ import annotations

from app.core.config import get_settings
from app.core.exceptions import AppException
from datetime import timedelta
import inspect
import logging
from urllib.parse import urlparse, urlunparse

settings = get_settings()
logger = logging.getLogger(__name__)
LIVEKIT_TOKEN_TTL_MINUTES = 30


def ensure_livekit_configured() -> None:
    if not settings.LIVEKIT_URL or not settings.LIVEKIT_API_KEY or not settings.LIVEKIT_API_SECRET:
        raise AppException("LiveKit is not configured on the server.", status_code=503)


def build_livekit_client_url() -> str:
    raw_url = str(settings.LIVEKIT_URL or "").strip()
    if not raw_url:
        raise AppException("LiveKit is not configured on the server.", status_code=503)

    parsed = urlparse(raw_url)
    if parsed.scheme in {"ws", "wss"}:
        return raw_url
    if parsed.scheme in {"http", "https"}:
        return urlunparse(
            parsed._replace(scheme="wss" if parsed.scheme == "https" else "ws")
        )
    return raw_url


def build_room_name(session_id: str) -> str:
    safe_session = str(session_id or "").strip()
    if not safe_session:
        raise AppException("Invalid session id", status_code=400)
    return f"{settings.LIVEKIT_ROOM_PREFIX}{safe_session}"


def build_call_room_name(call_session_id: str) -> str:
    safe_call_session = str(call_session_id or "").strip()
    if not safe_call_session:
        raise AppException("Invalid call session id", status_code=400)
    return f"{settings.LIVEKIT_ROOM_PREFIX}call-{safe_call_session}"


def build_request_room_name(visitor_request_id: str) -> str:
    safe_request = str(visitor_request_id or "").strip()
    if not safe_request:
        raise AppException("Invalid visitor request id", status_code=400)
    return f"{settings.LIVEKIT_ROOM_PREFIX}{safe_request}"


def build_livekit_identity(role: str, user_id: str) -> str:
    normalized_role = str(role or "").strip().lower()
    safe_user_id = str(user_id or "").strip()
    if normalized_role not in {"homeowner", "security", "visitor", "estate", "admin"} or not safe_user_id:
        raise AppException("Invalid LiveKit identity parameters.", status_code=400)
    return f"{normalized_role}_{safe_user_id}"


async def _close_livekit_client(client) -> None:
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if not callable(closer):
        return
    out = closer()
    if inspect.isawaitable(out):
        await out


async def create_livekit_room(room_name: str) -> None:
    ensure_livekit_configured()

    try:
        from livekit import api as livekit_api
    except Exception as exc:  # pragma: no cover - import guard for misconfigured installs
        raise AppException(f"LiveKit SDK import failed: {exc}", status_code=500)

    try:
        client = livekit_api.LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
        )
    except Exception as exc:
        raise AppException(f"LiveKit client init failed: {exc}", status_code=500)

    try:
        room_api = getattr(client, "room", None) or getattr(client, "room_service", None)
        if room_api is None or not hasattr(room_api, "create_room"):
            raise AppException("LiveKit room service unavailable in SDK", status_code=500)

        request_cls = getattr(livekit_api, "CreateRoomRequest", None)
        if request_cls is not None:
            request = request_cls(name=room_name)
            result = room_api.create_room(request)
        else:
            result = room_api.create_room({"name": room_name})

        if inspect.isawaitable(result):
            await result
        logger.info("livekit.room.created room_name=%s", room_name)
    except AppException:
        raise
    except Exception as exc:
        raise AppException(f"Failed to create LiveKit room: {exc}", status_code=502)
    finally:
        await _close_livekit_client(client)


async def delete_livekit_room(room_name: str) -> None:
    ensure_livekit_configured()

    try:
        from livekit import api as livekit_api
    except Exception as exc:  # pragma: no cover - import guard for misconfigured installs
        raise AppException(f"LiveKit SDK import failed: {exc}", status_code=500)

    try:
        client = livekit_api.LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
        )
    except Exception as exc:
        raise AppException(f"LiveKit client init failed: {exc}", status_code=500)

    try:
        room_api = getattr(client, "room", None) or getattr(client, "room_service", None)
        if room_api is None or not hasattr(room_api, "delete_room"):
            raise AppException("LiveKit room service unavailable in SDK", status_code=500)

        request_cls = getattr(livekit_api, "DeleteRoomRequest", None)
        if request_cls is not None:
            request = request_cls(room=room_name)
            result = room_api.delete_room(request)
        else:
            result = room_api.delete_room({"room": room_name})

        if inspect.isawaitable(result):
            await result
        logger.info("livekit.room.deleted room_name=%s", room_name)
    except AppException:
        raise
    except Exception as exc:
        raise AppException(f"Failed to delete LiveKit room: {exc}", status_code=502)
    finally:
        await _close_livekit_client(client)


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
    access_token = livekit_api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
    expires_in_seconds = LIVEKIT_TOKEN_TTL_MINUTES * 60
    if hasattr(access_token, "with_ttl"):
        access_token = access_token.with_ttl(timedelta(minutes=LIVEKIT_TOKEN_TTL_MINUTES))
    token = (
        access_token
        .with_identity(identity)
        .with_name(display_name or identity)
        .with_grants(grants)
        .to_jwt()
    )
    logger.info(
        "livekit.token.issued room_name=%s identity=%s can_publish=%s can_subscribe=%s",
        room_name,
        identity,
        can_publish,
        can_subscribe,
    )
    return {
        "url": build_livekit_client_url(),
        "roomName": room_name,
        "token": token,
        "expiresIn": expires_in_seconds,
    }


def issue_livekit_token_for_room(
    *,
    room_name: str,
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

    grants = livekit_api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
    )
    access_token = livekit_api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
    expires_in_seconds = LIVEKIT_TOKEN_TTL_MINUTES * 60
    if hasattr(access_token, "with_ttl"):
        access_token = access_token.with_ttl(timedelta(minutes=LIVEKIT_TOKEN_TTL_MINUTES))
    token = (
        access_token
        .with_identity(identity)
        .with_name(display_name or identity)
        .with_grants(grants)
        .to_jwt()
    )
    logger.info(
        "livekit.token.issued room_name=%s identity=%s can_publish=%s can_subscribe=%s",
        room_name,
        identity,
        can_publish,
        can_subscribe,
    )
    return {
        "url": build_livekit_client_url(),
        "roomName": room_name,
        "token": token,
        "expiresIn": expires_in_seconds,
    }
