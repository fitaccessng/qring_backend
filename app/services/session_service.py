from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db.models import Door, Estate, Home, HomeownerSetting, VisitorSession
from app.services.security_service import evaluate_session_intelligence
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
    request_id: str | None = None,
    visitor_phone: str | None = None,
    purpose: str | None = None,
    photo_url: str | None = None,
    visitor_type: str | None = None,
    delivery_option: str | None = None,
    request_source: str | None = None,
    creator_role: str | None = None,
) -> VisitorSession:
    if request_id:
        existing = (
            db.query(VisitorSession)
            .filter(VisitorSession.request_id == request_id, VisitorSession.qr_id == qr_id)
            .order_by(VisitorSession.started_at.desc())
            .first()
        )
        if existing:
            desired_label = (visitor_label or "Visitor").strip() or "Visitor"
            if existing.visitor_label != desired_label:
                existing.visitor_label = desired_label
                db.commit()
                db.refresh(existing)
            return existing

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
    estate_id = home.estate_id if home else None
    gate_id = (door.gate_label if door else None) or "main-gate"
    clean_visitor_type = (visitor_type or "guest").strip().lower() or "guest"
    clean_delivery_option = (delivery_option or "").strip().lower() or None
    clean_request_source = (request_source or "visitor_qr").strip().lower() or "visitor_qr"
    clean_creator_role = (creator_role or "visitor").strip().lower() or "visitor"
    session = VisitorSession(
        request_id=request_id,
        qr_id=qr_id,
        home_id=home.id if home else qr_home_id,
        door_id=door.id if door else selected_door,
        homeowner_id=homeowner_id,
        appointment_id=appointment_id,
        visitor_label=visitor_label,
        visitor_phone=(visitor_phone or "").strip() or None,
        purpose=(purpose or "").strip() or None,
        photo_url=(photo_url or "").strip() or None,
        visitor_type=clean_visitor_type if clean_visitor_type in {"guest", "delivery"} else "guest",
        request_source=clean_request_source if clean_request_source in {"visitor_qr", "gateman_assisted"} else "visitor_qr",
        creator_role=clean_creator_role if clean_creator_role in {"visitor", "security"} else "visitor",
        estate_id=estate_id,
        gate_id=gate_id,
        status="submitted",
        communication_status="none",
        gate_status="waiting",
        delivery_option=clean_delivery_option,
        state_updated_at=datetime.utcnow(),
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if request_id:
            existing = (
                db.query(VisitorSession)
                .filter(VisitorSession.request_id == request_id, VisitorSession.qr_id == qr_id)
                .order_by(VisitorSession.started_at.desc())
                .first()
            )
            if existing:
                return existing
        raise
    estate = db.query(Estate).filter(Estate.id == estate_id).first() if estate_id else None
    homeowner_settings = (
        db.query(HomeownerSetting).filter(HomeownerSetting.user_id == homeowner_id).first() if homeowner_id else None
    )
    evaluate_session_intelligence(db, session, estate=estate, homeowner_settings=homeowner_settings)
    if session.auto_approved:
        session.status = "approved"
        session.homeowner_decision_at = datetime.utcnow()
        session.pre_approved = True
        session.pre_approved_reason = session.pre_approved_reason or "Auto-approved by trust rule"
    db.commit()
    db.refresh(session)
    return session


def mark_session_status(db: Session, session_id: str, status: str) -> VisitorSession | None:
    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        return None
    session.status = status
    session.state_updated_at = datetime.utcnow()
    if status in {"rejected", "closed", "completed"}:
        session.ended_at = session.ended_at or datetime.utcnow()
    if status == "approved":
        session.homeowner_decision_at = session.homeowner_decision_at or datetime.utcnow()
    if status == "rejected":
        session.homeowner_decision_at = session.homeowner_decision_at or datetime.utcnow()
        session.gate_status = session.gate_status or "denied_at_gate"
    db.commit()
    db.refresh(session)
    return session
