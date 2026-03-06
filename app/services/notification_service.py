from datetime import datetime
import json

from sqlalchemy.orm import Session

from app.db.models import Notification


def create_notification(db: Session, user_id: str, kind: str, payload: dict) -> Notification:
    notification = Notification(
        user_id=user_id,
        kind=kind,
        payload=json.dumps(payload),
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification


def list_notifications(db: Session, user_id: str) -> list[dict]:
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "payload": row.payload,
            "readAt": row.read_at.isoformat() if row.read_at else None,
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]


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
    return {
        "id": row.id,
        "kind": row.kind,
        "payload": row.payload,
        "readAt": row.read_at.isoformat() if row.read_at else None,
        "createdAt": row.created_at.isoformat(),
    }


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
    return len(rows)


def clear_all_notifications(db: Session, user_id: str) -> int:
    deleted = db.query(Notification).filter(Notification.user_id == user_id).delete(synchronize_session=False)
    db.commit()
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
