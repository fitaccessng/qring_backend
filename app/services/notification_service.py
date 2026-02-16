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
