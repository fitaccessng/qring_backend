from __future__ import annotations

import json
import asyncio
import logging

from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.core.redis import get_redis_client, prefixed_key
from app.db.models import Appointment, Notification, VisitorSession
from app.services.provider_integrations import send_push_fcm
from app.services.realtime_notification_service import (
    build_notification_envelope,
    build_notification_idempotency_key,
    emit_dashboard_notification,
)

logger = logging.getLogger(__name__)


def _schedule_dashboard_emit(func, /, *args, **kwargs) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(func(*args, **kwargs))
        return
    asyncio.create_task(func(*args, **kwargs))


def _claim_notification_create_once(idempotency_key: str, *, ttl_seconds: int = 60 * 60 * 6) -> bool:
    normalized = str(idempotency_key or "").strip()
    if not normalized:
        return True
    client = get_redis_client()
    if client is None:
        return True
    try:
        return bool(
            client.set(
                prefixed_key("notifications", "create", normalized),
                "1",
                ex=max(60, int(ttl_seconds)),
                nx=True,
            )
        )
    except Exception:
        return True


def _safe_json_payload(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def create_notification(
    db: Session,
    user_id: str,
    kind: str,
    payload: dict,
    *,
    idempotency_key: str | None = None,
    source: str = "notification_service",
) -> Notification | None:
    event_type = str(kind or payload.get("type") or "notification").strip() or "notification"
    session_id = str(payload.get("sessionId") or payload.get("session_id") or "").strip() or None
    effective_key = str(
        idempotency_key
        or payload.get("idempotencyKey")
        or build_notification_idempotency_key(
            event_type=event_type,
            user_id=user_id,
            session_id=session_id,
            entity_id=str(payload.get("appointmentId") or payload.get("callSessionId") or payload.get("snapshotId") or ""),
            action=str(payload.get("status") or payload.get("action") or ""),
        )
    ).strip()
    if effective_key and not _claim_notification_create_once(effective_key):
        logger.info(
            "notification.create.skipped_duplicate user_id=%s kind=%s idempotency_key=%s source=%s",
            user_id,
            kind,
            effective_key,
            source,
        )
        return None
    envelope = build_notification_envelope(
        notification_id=None,
        event_type=event_type,
        idempotency_key=effective_key,
        session_id=session_id,
        user_id=user_id,
        source=source,
        payload=payload,
    )
    notification = Notification(
        user_id=user_id,
        kind=kind,
        payload=json.dumps(envelope),
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    envelope["notificationId"] = notification.id
    try:
        message = str((envelope or {}).get("message") or "You have a new alert.")
        route = str((envelope or {}).get("route") or "")
        panic_id = str((envelope or {}).get("panicId") or "")
        action_set = str((envelope or {}).get("actionSet") or ("panic_response" if kind == "safety.panic" and panic_id else ""))
        title_map = {
            "visitor.request": "New Visitor Request",
            "estate.alert": "Estate Alert",
            "estate.invite": "Estate Invitation",
            "estate.assignment": "Door Assignment",
            "estate.payment.status": "Payment Status",
            "safety.panic": "Panic Alert Near You",
            "safety.panic.response": "Responder Update",
            "safety.panic.reported": "Panic Alert Review",
        }
        send_push_fcm(
            db,
            user_id=user_id,
            title=title_map.get(kind, "Qring Alert"),
            body=message,
            data={
                "kind": kind,
                "notificationId": notification.id,
                "eventId": envelope.get("eventId"),
                "idempotencyKey": envelope.get("idempotencyKey"),
                "type": envelope.get("type"),
                "sessionId": str((envelope or {}).get("sessionId") or ""),
                "alertId": str((envelope or {}).get("alertId") or ""),
                "panicId": panic_id,
                "route": route,
                "actionSet": action_set,
                "title": title_map.get(kind, "Qring Alert"),
                "body": message,
            },
        )
    except Exception:
        # Push failures must not block notification creation.
        pass
    payload_for_socket = {
        "id": notification.id,
        "kind": notification.kind,
        "payload": notification.payload,
        "readAt": None,
        "createdAt": notification.created_at.isoformat(),
        "notificationId": notification.id,
        "eventId": envelope.get("eventId"),
        "idempotencyKey": envelope.get("idempotencyKey"),
        "type": envelope.get("type"),
        "sessionId": envelope.get("sessionId"),
        "userId": user_id,
        "timestamp": envelope.get("timestamp"),
        "source": source,
    }
    notification.payload = json.dumps(envelope)
    db.commit()
    db.refresh(notification)
    db_payload = {
        **payload_for_socket,
        "payload": notification.payload,
    }
    _schedule_dashboard_emit(
        emit_dashboard_notification,
        event_name="notification.created",
        rooms=[
            "notifications",
            f"user:{user_id}",
            f"user:{user_id}:notifications",
        ],
        payload=db_payload,
        idempotency_key=f"dashboard:notification.created:{notification.id}",
        source=source,
    )
    return notification


def list_notifications(db: Session, user_id: str) -> list[dict]:
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    appointment_ids: set[str] = set()
    session_ids: set[str] = set()
    parsed_payloads: dict[str, dict] = {}
    for row in rows:
        payload_raw = row.payload or "{}"
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        parsed_payloads[row.id] = payload
        appointment_id = str(payload.get("appointmentId") or "").strip()
        session_id = str(payload.get("sessionId") or "").strip()
        if appointment_id:
            appointment_ids.add(appointment_id)
        if session_id:
            session_ids.add(session_id)

    appointments_by_id: dict[str, Appointment] = {}
    if appointment_ids:
        for appt in db.query(Appointment).filter(Appointment.id.in_(list(appointment_ids))).all():
            appointments_by_id[appt.id] = appt

    sessions_by_id: dict[str, VisitorSession] = {}
    if session_ids:
        for session in db.query(VisitorSession).filter(VisitorSession.id.in_(list(session_ids))).all():
            sessions_by_id[session.id] = session

    def _should_hide(payload: dict, kind: str) -> bool:
        appointment_id = str(payload.get("appointmentId") or "").strip()
        if appointment_id:
            appt = appointments_by_id.get(appointment_id)
            if appt:
                if appt.status in {"completed", "cancelled", "expired"}:
                    return True
                if kind == "appointment.accepted" and appt.status in {"arrived", "active"}:
                    return True
                if kind == "appointment.arrival" and appt.status in {"active"}:
                    return True
        session_id = str(payload.get("sessionId") or "").strip()
        if session_id:
            session = sessions_by_id.get(session_id)
            if session and session.status in {"closed", "completed", "rejected"}:
                return True
        return False

    items = []
    dedupe_seen: set[str] = set()
    for row in rows:
        payload = parsed_payloads.get(row.id) or {}
        if _should_hide(payload, row.kind):
            continue
        dedupe_key = f"{row.kind}|{str(payload.get('sessionId') or '').strip()}|{str(payload.get('appointmentId') or '').strip()}|{str(payload.get('message') or '').strip()}"
        if dedupe_key in dedupe_seen:
            continue
        dedupe_seen.add(dedupe_key)
        items.append(
            {
                "id": row.id,
                "kind": row.kind,
                "payload": row.payload,
                "readAt": row.read_at.isoformat() if row.read_at else None,
                "createdAt": row.created_at.isoformat(),
                "notificationId": str(payload.get("notificationId") or row.id),
                "eventId": str(payload.get("eventId") or row.id),
                "idempotencyKey": str(payload.get("idempotencyKey") or row.id),
                "type": str(payload.get("type") or row.kind),
                "sessionId": str(payload.get("sessionId") or "").strip() or None,
                "userId": row.user_id,
                "timestamp": payload.get("timestamp"),
            }
        )
        if len(items) >= 50:
            break
    return items


def mark_notification_read(db: Session, user_id: str, notification_id: str) -> dict | None:
    row = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.user_id == user_id)
        .first()
    )
    if not row:
        return None
    row.read_at = row.read_at or utc_now()
    db.commit()
    db.refresh(row)
    parsed_payload = _safe_json_payload(row.payload)
    payload = {
        "id": row.id,
        "kind": row.kind,
        "payload": row.payload,
        "readAt": row.read_at.isoformat() if row.read_at else None,
        "createdAt": row.created_at.isoformat(),
    }
    _schedule_dashboard_emit(
        emit_dashboard_notification,
        event_name="notification.updated",
        rooms=["notifications", f"user:{user_id}", f"user:{user_id}:notifications"],
        payload={
            **payload,
            "notificationId": row.id,
            "eventId": row.id,
            "idempotencyKey": f"notification.updated:{row.id}:{payload['readAt']}",
            "type": row.kind,
            "sessionId": str(parsed_payload.get("sessionId") or "").strip() or None,
            "userId": user_id,
        },
        idempotency_key=f"dashboard:notification.updated:{row.id}:{payload['readAt']}",
        source="notification_service.mark_read",
    )
    return payload


def mark_all_notifications_read(db: Session, user_id: str) -> int:
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.read_at.is_(None))
        .all()
    )
    if not rows:
        return 0
    now = utc_now()
    for row in rows:
        row.read_at = now
    db.commit()
    _schedule_dashboard_emit(
        emit_dashboard_notification,
        event_name="notifications.updated",
        rooms=["notifications", f"user:{user_id}", f"user:{user_id}:notifications"],
        payload=build_notification_envelope(
            event_type="notifications.updated",
            idempotency_key=f"notifications.read_all:{user_id}:{now.isoformat()}",
            user_id=user_id,
            source="notification_service.mark_all_read",
            payload={"action": "read_all", "updated": len(rows), "readAt": now.isoformat()},
        ),
        idempotency_key=f"dashboard:notifications.read_all:{user_id}:{now.isoformat()}",
        source="notification_service.mark_all_read",
    )
    return len(rows)


def clear_all_notifications(db: Session, user_id: str) -> int:
    deleted = db.query(Notification).filter(Notification.user_id == user_id).delete(synchronize_session=False)
    db.commit()
    _schedule_dashboard_emit(
        emit_dashboard_notification,
        event_name="notifications.updated",
        rooms=["notifications", f"user:{user_id}", f"user:{user_id}:notifications"],
        payload=build_notification_envelope(
            event_type="notifications.updated",
            idempotency_key=f"notifications.clear_all:{user_id}:{int(deleted or 0)}",
            user_id=user_id,
            source="notification_service.clear_all",
            payload={"action": "clear_all", "deleted": int(deleted or 0)},
        ),
        idempotency_key=f"dashboard:notifications.clear_all:{user_id}:{int(deleted or 0)}",
        source="notification_service.clear_all",
    )
    return int(deleted or 0)


def mark_session_notifications_read(
    db: Session,
    *,
    user_id: str,
    session_id: str | None = None,
    appointment_id: str | None = None,
) -> int:
    target_session = str(session_id or "").strip()
    target_appointment = str(appointment_id or "").strip()
    if not target_session and not target_appointment:
        return 0

    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.read_at.is_(None))
        .all()
    )
    if not rows:
        return 0

    now = utc_now()
    updated = 0
    for row in rows:
        payload_raw = row.payload or "{}"
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        payload_session = str(payload.get("sessionId") or "").strip()
        payload_appointment = str(payload.get("appointmentId") or "").strip()
        matches_session = bool(target_session and payload_session and payload_session == target_session)
        matches_appointment = bool(
            target_appointment and payload_appointment and payload_appointment == target_appointment
        )
        if matches_session or matches_appointment:
            row.read_at = now
            updated += 1

    if updated:
        db.commit()
    return updated
