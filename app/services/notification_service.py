from __future__ import annotations

import json
import asyncio
from datetime import datetime

from anyio import from_thread

from sqlalchemy.orm import Session

from app.db.models import Appointment, Notification, VisitorSession
from app.core.config import get_settings
from app.socket.server import sio
from app.services.provider_integrations import send_push_fcm

settings = get_settings()


def _emit_notification_event(*, event: str, user_id: str, payload: dict) -> None:
    rooms = [
        "notifications",
        f"user:{user_id}",
        f"user:{user_id}:notifications",
    ]

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        for room in rooms:
            try:
                from_thread.run(
                    sio.emit,
                    event,
                    payload,
                    room=room,
                    namespace=settings.DASHBOARD_NAMESPACE,
                )
            except Exception:
                return
        return

    for room in rooms:
        sio.start_background_task(
            sio.emit,
            event,
            payload,
            room=room,
            namespace=settings.DASHBOARD_NAMESPACE,
        )


def create_notification(db: Session, user_id: str, kind: str, payload: dict) -> Notification:
    notification = Notification(
        user_id=user_id,
        kind=kind,
        payload=json.dumps(payload),
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    try:
        message = str((payload or {}).get("message") or "You have a new alert.")
        title_map = {
            "visitor.request": "New Visitor Request",
            "estate.alert": "Estate Alert",
            "estate.invite": "Estate Invitation",
            "estate.assignment": "Door Assignment",
            "estate.payment.status": "Payment Status",
        }
        send_push_fcm(
            db,
            user_id=user_id,
            title=title_map.get(kind, "Qring Alert"),
            body=message,
            data={
                "kind": kind,
                "notificationId": notification.id,
                "sessionId": str((payload or {}).get("sessionId") or ""),
                "alertId": str((payload or {}).get("alertId") or ""),
            },
        )
    except Exception:
        # Push failures must not block notification creation.
        pass
    _emit_notification_event(
        event="notification.created",
        user_id=user_id,
        payload={
            "id": notification.id,
            "kind": notification.kind,
            "payload": notification.payload,
            "readAt": None,
            "createdAt": notification.created_at.isoformat(),
        },
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
    row.read_at = row.read_at or datetime.utcnow()
    db.commit()
    db.refresh(row)
    payload = {
        "id": row.id,
        "kind": row.kind,
        "payload": row.payload,
        "readAt": row.read_at.isoformat() if row.read_at else None,
        "createdAt": row.created_at.isoformat(),
    }
    _emit_notification_event(event="notification.updated", user_id=user_id, payload=payload)
    return payload


def mark_all_notifications_read(db: Session, user_id: str) -> int:
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.read_at.is_(None))
        .all()
    )
    if not rows:
        return 0
    now = datetime.utcnow()
    for row in rows:
        row.read_at = now
    db.commit()
    _emit_notification_event(
        event="notifications.updated",
        user_id=user_id,
        payload={"action": "read_all", "updated": len(rows), "readAt": now.isoformat()},
    )
    return len(rows)


def clear_all_notifications(db: Session, user_id: str) -> int:
    deleted = db.query(Notification).filter(Notification.user_id == user_id).delete(synchronize_session=False)
    db.commit()
    _emit_notification_event(
        event="notifications.updated",
        user_id=user_id,
        payload={"action": "clear_all", "deleted": int(deleted or 0)},
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

    now = datetime.utcnow()
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
