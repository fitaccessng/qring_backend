from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from anyio import from_thread

from app.core.config import get_settings
from app.core.redis import get_async_redis_client, prefixed_key
from app.socket.server import sio

settings = get_settings()
logger = logging.getLogger(__name__)

DEFAULT_NOTIFICATION_TTL_SECONDS = 60 * 60 * 6


def _utc_timestamp_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def build_notification_envelope(
    *,
    notification_id: str | None = None,
    event_id: str | None = None,
    idempotency_key: str | None = None,
    event_type: str,
    session_id: str | None = None,
    user_id: str | None = None,
    source: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_payload = dict(payload or {})
    effective_event_id = str(event_id or normalized_payload.get("eventId") or uuid.uuid4())
    effective_notification_id = str(
        notification_id or normalized_payload.get("notificationId") or effective_event_id
    )
    effective_idempotency_key = str(
        idempotency_key
        or normalized_payload.get("idempotencyKey")
        or normalized_payload.get("clientId")
        or effective_event_id
    )
    effective_session_id = str(
        session_id or normalized_payload.get("sessionId") or normalized_payload.get("session_id") or ""
    ).strip()
    effective_user_id = str(user_id or normalized_payload.get("userId") or "").strip()

    return {
        **normalized_payload,
        "notificationId": effective_notification_id,
        "eventId": effective_event_id,
        "idempotencyKey": effective_idempotency_key,
        "type": str(event_type or normalized_payload.get("type") or "").strip(),
        "sessionId": effective_session_id or None,
        "userId": effective_user_id or None,
        "timestamp": int(normalized_payload.get("timestamp") or _utc_timestamp_ms()),
        "source": str(source or normalized_payload.get("source") or "").strip() or None,
    }


def build_notification_idempotency_key(
    *,
    event_type: str,
    user_id: str | None = None,
    session_id: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
) -> str:
    parts = [
        str(event_type or "").strip(),
        str(user_id or "").strip(),
        str(session_id or "").strip(),
        str(entity_id or "").strip(),
        str(action or "").strip(),
    ]
    return "|".join(part for part in parts if part)


async def claim_notification_idempotency(
    idempotency_key: str,
    *,
    ttl_seconds: int = DEFAULT_NOTIFICATION_TTL_SECONDS,
) -> bool:
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_key:
        return True

    redis = get_async_redis_client()
    if redis is None:
        return True

    try:
        claimed = await redis.set(
            prefixed_key("notifications", "idempotency", normalized_key),
            "1",
            ex=max(60, int(ttl_seconds)),
            nx=True,
        )
        return bool(claimed)
    except Exception:
        logger.exception("notification.idempotency.redis_failed key=%s", normalized_key)
        return True


async def emit_socket_notification(
    *,
    event_name: str,
    namespace: str,
    rooms: list[str] | tuple[str, ...] | set[str] | None = None,
    to: str | None = None,
    payload: dict[str, Any],
    idempotency_key: str,
    source: str,
    ttl_seconds: int = DEFAULT_NOTIFICATION_TTL_SECONDS,
) -> bool:
    claimed = await claim_notification_idempotency(idempotency_key, ttl_seconds=ttl_seconds)
    if not claimed:
        logger.info(
            "notification.emit.skipped_duplicate event=%s idempotency_key=%s source=%s",
            event_name,
            idempotency_key,
            source,
        )
        return False

    targets = list(dict.fromkeys([room for room in (rooms or []) if room]))
    logger.info(
        "notification.emit event=%s notification_id=%s event_id=%s idempotency_key=%s source=%s rooms=%s to=%s",
        event_name,
        payload.get("notificationId"),
        payload.get("eventId"),
        idempotency_key,
        source,
        ",".join(targets),
        to or "",
    )

    async def _emit() -> None:
        if to:
            await sio.emit(event_name, payload, to=to, namespace=namespace)
            return
        for room in targets:
            await sio.emit(event_name, payload, room=room, namespace=namespace)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            from_thread.run(_emit)
        except Exception:
            logger.exception("notification.emit.failed event=%s source=%s", event_name, source)
            return False
        return True

    try:
        await _emit()
        return True
    except Exception:
        logger.exception("notification.emit.failed event=%s source=%s", event_name, source)
        return False


async def emit_dashboard_notification(
    *,
    event_name: str,
    payload: dict[str, Any],
    idempotency_key: str,
    source: str,
    rooms: list[str] | tuple[str, ...] | set[str] | None = None,
    to: str | None = None,
    ttl_seconds: int = DEFAULT_NOTIFICATION_TTL_SECONDS,
) -> bool:
    return await emit_socket_notification(
        event_name=event_name,
        namespace=settings.DASHBOARD_NAMESPACE,
        rooms=rooms,
        to=to,
        payload=payload,
        idempotency_key=idempotency_key,
        source=source,
        ttl_seconds=ttl_seconds,
    )


async def emit_signaling_notification(
    *,
    event_name: str,
    payload: dict[str, Any],
    idempotency_key: str,
    source: str,
    rooms: list[str] | tuple[str, ...] | set[str] | None = None,
    to: str | None = None,
    ttl_seconds: int = DEFAULT_NOTIFICATION_TTL_SECONDS,
) -> bool:
    return await emit_socket_notification(
        event_name=event_name,
        namespace=settings.SIGNALING_NAMESPACE,
        rooms=rooms,
        to=to,
        payload=payload,
        idempotency_key=idempotency_key,
        source=source,
        ttl_seconds=ttl_seconds,
    )
