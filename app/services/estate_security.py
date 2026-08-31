from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Estate, User, UserRole


def estate_has_security(db: Session, estate_id: str | None, *, gate_id: str | None = None) -> bool:
    """Return whether an estate is configured to use the security path."""
    normalized_estate_id = str(estate_id or "").strip()
    if not normalized_estate_id:
        return False

    estate = db.query(Estate).filter(Estate.id == normalized_estate_id).first()
    return bool(estate) and bool(getattr(estate, "security_enabled", True))


def estate_security_available(db: Session, estate_id: str | None, *, gate_id: str | None = None) -> bool:
    """Return whether a configured estate currently has an active security account."""
    normalized_estate_id = str(estate_id or "").strip()
    if not normalized_estate_id or not estate_has_security(db, normalized_estate_id):
        return False

    query = db.query(User).filter(
        User.role == UserRole.security,
        User.estate_id == normalized_estate_id,
        User.is_active.is_(True),
    )
    normalized_gate_id = str(gate_id or "").strip()
    if normalized_gate_id:
        gate_match = query.filter((User.gate_id == normalized_gate_id) | (User.gate_id.is_(None))).first()
        if gate_match:
            return True

    return query.first() is not None
