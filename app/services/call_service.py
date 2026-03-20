import uuid
from datetime import datetime
import logging

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import Appointment, CallSession, User, VisitorSession
from app.services.payment_service import require_subscription_feature
from app.services.livekit_service import (
    build_call_room_name,
    create_livekit_room,
    delete_livekit_room,
    issue_livekit_token_for_room,
)
from app.services.notification_service import create_notification

CALL_ACTIVE_STATUSES = {"pending", "ringing", "active"}
logger = logging.getLogger(__name__)


def _validate_appointment_for_call(appointment: Appointment) -> None:
    if appointment.status in {"cancelled", "completed", "closed", "ended", "rejected"}:
        raise AppException("Appointment is not valid for calling.", status_code=409)
    if appointment.ends_at and appointment.ends_at < datetime.utcnow():
        raise AppException("Appointment has expired.", status_code=409)


async def start_call_session(
    db: Session,
    *,
    appointment_id: str | None = None,
    visitor_session_id: str | None = None,
    visitor_id: str | None = None,
    homeowner_id: str | None = None,
    visitor_name: str | None = None,
) -> CallSession:
    effective_appointment_id = str(appointment_id or "").strip()
    effective_homeowner_id = str(homeowner_id or "").strip()
    visitor_session = None
    session_id = str(visitor_session_id or "").strip()
    if session_id:
        visitor_session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
        if not visitor_session:
            raise AppException("Visitor session not found.", status_code=404)
        if homeowner_id and visitor_session.homeowner_id != homeowner_id:
            raise AppException("You are not allowed to start this call.", status_code=403)
        if not effective_homeowner_id:
            effective_homeowner_id = visitor_session.homeowner_id
        if not effective_appointment_id and visitor_session.appointment_id:
            effective_appointment_id = visitor_session.appointment_id

    if not effective_appointment_id and not visitor_session:
        raise AppException("appointmentId or sessionId is required.", status_code=400)

    appointment = None
    if effective_appointment_id:
        appointment = db.query(Appointment).filter(Appointment.id == effective_appointment_id).first()
        if not appointment:
            raise AppException("Appointment not found.", status_code=404)
        if homeowner_id and appointment.homeowner_id != homeowner_id:
            raise AppException("You are not allowed to start this call.", status_code=403)
        _validate_appointment_for_call(appointment)
        effective_homeowner_id = appointment.homeowner_id

    if not effective_homeowner_id:
        raise AppException("Homeowner context is required to start call.", status_code=400)
    require_subscription_feature(db, effective_homeowner_id, "chat_call_verification", user_role="homeowner")

    visitor_identity = str(visitor_id or "").strip()
    if not visitor_identity and visitor_session:
        visitor_identity = visitor_session.id
    if not visitor_identity and appointment:
        visitor_identity = f"appointment:{appointment.id}"
    if not visitor_identity and visitor_session:
        visitor_identity = visitor_session.id

    existing_query = db.query(CallSession).filter(CallSession.status.in_(CALL_ACTIVE_STATUSES))
    if appointment:
        existing_query = existing_query.filter(CallSession.appointment_id == appointment.id)
    elif visitor_session:
        existing_query = existing_query.filter(CallSession.visitor_session_id == visitor_session.id)
    else:
        existing_query = existing_query.filter(
            CallSession.homeowner_id == effective_homeowner_id,
            CallSession.visitor_id == visitor_identity,
        )
    existing = existing_query.order_by(CallSession.created_at.desc()).first()
    if existing:
        if existing.visitor_id != visitor_identity:
            raise AppException("Call already in progress for this session.", status_code=409)
        logger.info(
            "call.start.reused_existing call_session_id=%s appointment_id=%s visitor_session_id=%s homeowner_id=%s",
            existing.id,
            existing.appointment_id,
            existing.visitor_session_id,
            existing.homeowner_id,
        )
        return existing

    call_session_id = str(uuid.uuid4())
    room_name = build_call_room_name(call_session_id)
    await create_livekit_room(room_name)

    row = CallSession(
        id=call_session_id,
        appointment_id=appointment.id if appointment else None,
        visitor_session_id=visitor_session.id if visitor_session else None,
        room_name=room_name,
        visitor_id=visitor_identity,
        homeowner_id=effective_homeowner_id,
        status="ringing",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    effective_visitor_name = (visitor_name or "").strip() or (
        appointment.visitor_name if appointment else (visitor_session.visitor_label if visitor_session else "Visitor")
    )
    create_notification(
        db=db,
        user_id=effective_homeowner_id,
        kind="call.request",
        payload={
            "callSessionId": row.id,
            "appointmentId": appointment.id if appointment else None,
            "sessionId": visitor_session.id if visitor_session else None,
            "roomName": row.room_name,
            "visitorId": row.visitor_id,
            "visitorName": effective_visitor_name,
            "message": f"{effective_visitor_name} is calling.",
        },
    )
    logger.info(
        "call.started call_session_id=%s appointment_id=%s visitor_session_id=%s homeowner_id=%s visitor_id=%s room_name=%s",
        row.id,
        row.appointment_id,
        row.visitor_session_id,
        row.homeowner_id,
        row.visitor_id,
        row.room_name,
    )
    return row


def _get_homeowner_display_name(db: Session, homeowner_id: str) -> str:
    user = db.query(User).filter(User.id == homeowner_id).first()
    return (user.full_name if user else "") or "Homeowner"


def _get_visitor_display_name(db: Session, call_session: CallSession) -> str:
    if call_session.appointment_id:
        appointment = db.query(Appointment).filter(Appointment.id == call_session.appointment_id).first()
        if appointment and appointment.visitor_name:
            return appointment.visitor_name
    if call_session.visitor_session_id:
        visit = db.query(VisitorSession).filter(VisitorSession.id == call_session.visitor_session_id).first()
        if visit and visit.visitor_label:
            return visit.visitor_label
    return "Visitor"


def join_call_as_homeowner(db: Session, *, call_session_id: str, homeowner_id: str) -> dict:
    require_subscription_feature(db, homeowner_id, "chat_call_verification", user_role="homeowner")
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise AppException("Call session not found.", status_code=404)
    if row.homeowner_id != homeowner_id:
        raise AppException("You are not allowed to join this call.", status_code=403)
    if row.status == "ended":
        raise AppException("Call has ended.", status_code=409)

    if row.status in {"pending", "ringing"}:
        row.status = "active"
        db.commit()
        db.refresh(row)

    data = issue_livekit_token_for_room(
        room_name=row.room_name,
        identity=f"homeowner:{homeowner_id}:call:{row.id}",
        display_name=_get_homeowner_display_name(db, homeowner_id),
        can_publish=True,
        can_subscribe=True,
    )
    logger.info(
        "call.join.homeowner call_session_id=%s homeowner_id=%s room_name=%s status=%s",
        row.id,
        homeowner_id,
        row.room_name,
        row.status,
    )
    return {"token": data["token"], "roomName": data["roomName"], "status": row.status}


def join_call_as_visitor(db: Session, *, call_session_id: str, visitor_id: str) -> dict:
    visitor_identity = str(visitor_id or "").strip()
    if not visitor_identity:
        raise AppException("visitorId is required.", status_code=400)

    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise AppException("Call session not found.", status_code=404)
    require_subscription_feature(db, row.homeowner_id, "chat_call_verification", user_role="homeowner")
    if row.visitor_id != visitor_identity:
        raise AppException("You are not allowed to join this call.", status_code=403)
    if row.status == "ended":
        raise AppException("Call has ended.", status_code=409)

    if row.status in {"pending", "ringing"}:
        row.status = "active"
        db.commit()
        db.refresh(row)

    data = issue_livekit_token_for_room(
        room_name=row.room_name,
        identity=f"visitor:{visitor_identity}:call:{row.id}",
        display_name=_get_visitor_display_name(db, row),
        can_publish=True,
        can_subscribe=True,
    )
    logger.info(
        "call.join.visitor call_session_id=%s visitor_id=%s room_name=%s status=%s",
        row.id,
        visitor_identity,
        row.room_name,
        row.status,
    )
    return {"token": data["token"], "roomName": data["roomName"], "status": row.status}


async def end_call_session(db: Session, *, call_session_id: str) -> CallSession:
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise AppException("Call session not found.", status_code=404)

    if row.status != "ended":
        row.status = "ended"
        row.ended_at = row.ended_at or datetime.utcnow()
        db.commit()
        db.refresh(row)
    logger.info(
        "call.ended call_session_id=%s appointment_id=%s visitor_session_id=%s room_name=%s",
        row.id,
        row.appointment_id,
        row.visitor_session_id,
        row.room_name,
    )

    try:
        await delete_livekit_room(row.room_name)
    except AppException:
        # Keep end-call idempotent even if room cleanup fails or room no longer exists.
        logger.exception("call.end.cleanup_failed call_session_id=%s room_name=%s", row.id, row.room_name)
        pass
    return row
