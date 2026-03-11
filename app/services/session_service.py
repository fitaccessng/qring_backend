from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Door, Home, VisitorSession
from app.services.door_routing_service import select_door


def create_visitor_session(
    db: Session,
    qr_id: str,
    qr_home_id: str,
    doors: list[str],
    mode: str,
    requested_door: str | None,
    visitor_label: str = "Visitor",
    appointment_id: str | None = None,
) -> VisitorSession:
    if appointment_id:
        existing = (
            db.query(VisitorSession)
            .filter(
                VisitorSession.appointment_id == appointment_id,
                VisitorSession.status.in_({"pending", "active", "approved"}),
            )
            .order_by(VisitorSession.started_at.desc())
            .first()
        )
        if existing:
            updated = False
            desired_label = (visitor_label or "Visitor").strip() or "Visitor"
            if existing.visitor_label != desired_label:
                existing.visitor_label = desired_label
                updated = True
            if updated:
                db.commit()
                db.refresh(existing)
            return existing

    selected_door = select_door(doors, mode, requested_door)
    door = db.query(Door).filter(Door.id == selected_door).first()
    home = db.query(Home).filter(Home.id == (door.home_id if door else qr_home_id)).first()

    homeowner_id = home.homeowner_id if home else ""
    session = VisitorSession(
        qr_id=qr_id,
        home_id=home.id if home else qr_home_id,
        door_id=door.id if door else selected_door,
        homeowner_id=homeowner_id,
        appointment_id=appointment_id,
        visitor_label=visitor_label,
        status="pending",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def mark_session_status(db: Session, session_id: str, status: str) -> VisitorSession | None:
    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        return None
    session.status = status
    if status in {"rejected", "closed", "completed"}:
        session.ended_at = session.ended_at or datetime.utcnow()
    db.commit()
    db.refresh(session)
    return session
