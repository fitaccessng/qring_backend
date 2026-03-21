from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import DigitalAccessPass, Door, GateLog, Home


def _serialize_access_pass(row: DigitalAccessPass) -> dict[str, Any]:
    return {
        "id": row.id,
        "passType": row.pass_type,
        "label": row.label,
        "visitorName": row.visitor_name,
        "codeValue": row.code_value,
        "validFrom": row.valid_from.isoformat() if row.valid_from else None,
        "validUntil": row.valid_until.isoformat() if row.valid_until else None,
        "maxUses": int(row.max_uses or 0),
        "usedCount": int(row.used_count or 0),
        "remainingUses": max(int(row.max_uses or 0) - int(row.used_count or 0), 0),
        "isActive": bool(row.is_active),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "doorId": row.door_id,
        "homeId": row.home_id,
    }


def list_homeowner_access_passes(db: Session, homeowner_id: str) -> list[dict[str, Any]]:
    rows = (
        db.query(DigitalAccessPass)
        .filter(DigitalAccessPass.homeowner_id == homeowner_id)
        .order_by(DigitalAccessPass.created_at.desc())
        .limit(60)
        .all()
    )
    return [_serialize_access_pass(row) for row in rows]


def create_homeowner_access_pass(
    db: Session,
    *,
    homeowner_id: str,
    label: str,
    pass_type: str,
    visitor_name: str | None = None,
    door_id: str | None = None,
    valid_for_hours: int = 24,
    max_uses: int = 1,
) -> dict[str, Any]:
    clean_type = (pass_type or "qr").strip().lower()
    if clean_type not in {"qr", "pin"}:
        raise AppException("passType must be qr or pin", status_code=400)
    clean_label = (label or "").strip() or "Guest Access"
    duration_hours = max(1, min(int(valid_for_hours or 24), 168))
    allowed_uses = max(1, min(int(max_uses or 1), 100))

    home = (
        db.query(Home)
        .filter(Home.homeowner_id == homeowner_id)
        .order_by(Home.created_at.asc())
        .first()
    )
    if not home:
        raise AppException("Homeowner home not found", status_code=404)

    door = None
    if door_id:
        door = (
            db.query(Door)
            .join(Home, Home.id == Door.home_id)
            .filter(Door.id == door_id, Home.homeowner_id == homeowner_id)
            .first()
        )
        if not door:
            raise AppException("Door not found for homeowner", status_code=404)

    code_value = (
        "".join(secrets.choice("0123456789") for _ in range(6))
        if clean_type == "pin"
        else f"acc_{secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:12]}"
    )
    now = datetime.utcnow()
    row = DigitalAccessPass(
        homeowner_id=homeowner_id,
        estate_id=home.estate_id,
        home_id=home.id,
        door_id=door.id if door else None,
        pass_type=clean_type,
        label=clean_label,
        visitor_name=(visitor_name or "").strip() or None,
        code_value=code_value,
        valid_from=now,
        valid_until=now + timedelta(hours=duration_hours),
        max_uses=allowed_uses,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_access_pass(row)


def deactivate_access_pass(db: Session, *, homeowner_id: str, access_pass_id: str) -> dict[str, Any]:
    row = (
        db.query(DigitalAccessPass)
        .filter(DigitalAccessPass.id == access_pass_id, DigitalAccessPass.homeowner_id == homeowner_id)
        .first()
    )
    if not row:
        raise AppException("Access pass not found", status_code=404)
    row.is_active = False
    db.commit()
    db.refresh(row)
    return _serialize_access_pass(row)


def validate_access_pass(
    db: Session,
    *,
    security_user_id: str,
    estate_id: str | None,
    gate_id: str | None,
    code_value: str,
) -> dict[str, Any]:
    clean_code = (code_value or "").strip()
    if not clean_code:
        raise AppException("Code is required", status_code=400)
    row = db.query(DigitalAccessPass).filter(DigitalAccessPass.code_value == clean_code).first()
    if not row:
        raise AppException("Access code not found", status_code=404)
    now = datetime.utcnow()
    if not row.is_active:
        raise AppException("Access code is no longer active", status_code=400)
    if row.estate_id and estate_id and row.estate_id != estate_id:
        raise AppException("Access code does not belong to this estate", status_code=403)
    if row.valid_from and now < row.valid_from:
        raise AppException("Access code is not active yet", status_code=400)
    if row.valid_until and now > row.valid_until:
        raise AppException("Access code has expired", status_code=400)
    if row.max_uses and row.used_count >= row.max_uses:
        raise AppException("Access code has already been used", status_code=400)

    row.used_count = int(row.used_count or 0) + 1
    if row.max_uses and row.used_count >= row.max_uses:
        row.is_active = False
    db.add(
        GateLog(
            visitor_session_id=None,
            estate_id=row.estate_id,
            home_id=row.home_id,
            gate_id=gate_id,
            actor_user_id=security_user_id,
            actor_role="security",
            action="digital_access_validated",
            resulting_status="approved",
            notes=f"{row.pass_type.upper()} access granted via digital pass",
            meta_json=f'{{"accessPassId":"{row.id}","codeValue":"{row.code_value}"}}',
        )
    )
    db.commit()
    db.refresh(row)
    return _serialize_access_pass(row)
