import json
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditLog


def write_audit_log(
    db: Session,
    actor_user_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        meta_json=json.dumps(meta or {}, ensure_ascii=True),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_audit_logs(db: Session, limit: int = 200) -> list[AuditLog]:
    return db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()

