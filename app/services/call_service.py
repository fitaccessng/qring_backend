from __future__ import annotations

import uuid
from datetime import datetime
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.core.time import ensure_utc, utc_now
from app.db.models import Appointment, CallSession, User, VisitorSession
try:
    from app.services.payment_service import require_subscription_feature
except Exception:  # pragma: no cover - local test dependency fallback
    def require_subscription_feature(*args, **kwargs):
        return {}
try:
    from app.services.notification_service import create_notification
except Exception:  # pragma: no cover - local test dependency fallback
    def create_notification(*args, **kwargs):
        return {}
from app.services.realtime_config_service import build_webrtc_rtc_config

CALL_SETUP_STATUSES = {"pending", "ringing"}
CALL_CONNECTED_STATUSES = {"active", "ongoing"}
CALL_TERMINAL_STATUSES = {"ended", "missed"}
CALL_ACTIVE_STATUSES = CALL_SETUP_STATUSES | CALL_CONNECTED_STATUSES
logger = logging.getLogger(__name__)


def _validate_appointment_for_call(appointment: Appointment) -> None:
    if appointment.status in {"cancelled", "completed", "closed", "ended", "rejected"}:
        raise AppException("Appointment is not valid for calling.", status_code=409)
    appointment_end = ensure_utc(appointment.ends_at)
    if appointment_end and appointment_end < utc_now():
        raise AppException("Appointment has expired.", status_code=409)


def _build_active_call_query(
    db: Session,
    *,
    appointment: Appointment | None,
    visitor_session: VisitorSession | None,
    effective_homeowner_id: str,
    visitor_identity: str,
):
    query = db.query(CallSession).filter(CallSession.status.in_(CALL_ACTIVE_STATUSES))
    if appointment:
        return query.filter(CallSession.appointment_id == appointment.id)
    if visitor_session:
        return query.filter(CallSession.visitor_session_id == visitor_session.id)
    return query.filter(
        CallSession.homeowner_id == effective_homeowner_id,
        CallSession.visitor_id == visitor_identity,
    )


def _find_existing_active_call(
    db: Session,
    *,
    appointment: Appointment | None,
    visitor_session: VisitorSession | None,
    effective_homeowner_id: str,
    visitor_identity: str,
) -> CallSession | None:
    return (
        _build_active_call_query(
            db,
            appointment=appointment,
            visitor_session=visitor_session,
            effective_homeowner_id=effective_homeowner_id,
            visitor_identity=visitor_identity,
        )
        .order_by(CallSession.created_at.desc())
        .first()
    )


async def start_call_session(
    db: Session,
    *,
    appointment_id: str | None = None,
    visitor_session_id: str | None = None,
    visitor_id: str | None = None,
    homeowner_id: str | None = None,
    security_user_id: str | None = None,
    caller_id: str | None = None,
    receiver_id: str | None = None,
    call_type: str = "audio",
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

    logger.info(
        "call.start.request appointment_id=%s visitor_session_id=%s homeowner_id=%s security_user_id=%s caller_id=%s call_type=%s visitor_id=%s",
        effective_appointment_id or None,
        visitor_session.id if visitor_session else None,
        effective_homeowner_id,
        str(security_user_id or "").strip() or None,
        str(caller_id or "").strip() or None,
        (call_type or "audio").strip() or "audio",
        visitor_identity or None,
    )

    existing = _find_existing_active_call(
        db,
        appointment=appointment,
        visitor_session=visitor_session,
        effective_homeowner_id=effective_homeowner_id,
        visitor_identity=visitor_identity,
    )
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
    visitor_request_id = (
        str(visitor_session.request_id or "").strip()
        if visitor_session and visitor_session.request_id
        else str(visitor_session.id if visitor_session else effective_appointment_id or visitor_identity).strip()
    )
    room_name = f"qring-call-{visitor_request_id}"

    row = CallSession(
        id=call_session_id,
        appointment_id=appointment.id if appointment else None,
        visitor_session_id=visitor_session.id if visitor_session else None,
        security_user_id=str(security_user_id or "").strip() or None,
        caller_id=str(caller_id or "").strip() or None,
        receiver_id=str(receiver_id or "").strip() or None,
        call_type=(call_type or "audio").strip() or "audio",
        room_name=room_name,
        visitor_id=visitor_identity,
        homeowner_id=effective_homeowner_id,
        visitor_request_id=visitor_request_id or None,
        initiated_by_role="security" if security_user_id else ("homeowner" if homeowner_id else None),
        status="ringing",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning(
            "call.start.integrity_conflict appointment_id=%s visitor_session_id=%s homeowner_id=%s room_name=%s",
            row.appointment_id,
            row.visitor_session_id,
            row.homeowner_id,
            row.room_name,
            exc_info=True,
        )
        existing = (
            db.query(CallSession)
            .filter(CallSession.room_name == row.room_name)
            .order_by(CallSession.created_at.desc())
            .first()
        ) or _find_existing_active_call(
            db,
            appointment=appointment,
            visitor_session=visitor_session,
            effective_homeowner_id=effective_homeowner_id,
            visitor_identity=visitor_identity,
        )
        if existing:
            logger.info(
                "call.start.recovered_existing call_session_id=%s appointment_id=%s visitor_session_id=%s homeowner_id=%s room_name=%s",
                existing.id,
                existing.appointment_id,
                existing.visitor_session_id,
                existing.homeowner_id,
                existing.room_name,
            )
            return existing
        raise AppException("Call session already exists for this visitor.", status_code=409)
    except Exception:
        db.rollback()
        logger.exception(
            "call.start.commit_failed appointment_id=%s visitor_session_id=%s homeowner_id=%s room_name=%s",
            row.appointment_id,
            row.visitor_session_id,
            row.homeowner_id,
            row.room_name,
        )
        raise
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
            "visitorRequestId": row.visitor_request_id,
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


def mark_call_session_answered(db: Session, *, call_session_id: str) -> CallSession | None:
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        return None
    if row.status in CALL_TERMINAL_STATUSES:
        return row
    if row.status not in CALL_CONNECTED_STATUSES:
        row.status = "ongoing"
        row.answered_at = row.answered_at or utc_now()
        db.commit()
        db.refresh(row)
    return row


def mark_call_session_rejected(db: Session, *, call_session_id: str, reason: str = "rejected") -> CallSession | None:
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        return None
    if row.status in CALL_TERMINAL_STATUSES:
        return row
    row.status = "missed" if row.status in CALL_SETUP_STATUSES else "ended"
    row.ended_at = row.ended_at or utc_now()
    row.ended_reason = str(reason or "").strip() or "rejected"
    db.commit()
    db.refresh(row)
    return row


def join_call_as_homeowner(db: Session, *, call_session_id: str, homeowner_id: str) -> dict:
    require_subscription_feature(db, homeowner_id, "chat_call_verification", user_role="homeowner")
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise AppException("Call session not found.", status_code=404)
    if row.homeowner_id != homeowner_id:
        raise AppException("You are not allowed to join this call.", status_code=403)
    if row.status in CALL_TERMINAL_STATUSES:
        raise AppException("Call has ended.", status_code=409)
    logger.info(
        "call.join.homeowner call_session_id=%s homeowner_id=%s room_name=%s status=%s",
        row.id,
        homeowner_id,
        row.room_name,
        row.status,
    )
    return {
        "callSessionId": row.id,
        "roomName": row.room_name,
        "status": row.status,
        "displayName": _get_homeowner_display_name(db, homeowner_id),
        "rtcConfig": build_webrtc_rtc_config(),
    }


def join_call_as_security(db: Session, *, call_session_id: str, security_user_id: str) -> dict:
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise AppException("Call session not found.", status_code=404)
    if row.security_user_id != security_user_id:
        raise AppException("You are not allowed to join this call.", status_code=403)
    if row.status in CALL_TERMINAL_STATUSES:
        raise AppException("Call has ended.", status_code=409)
    security_user = db.query(User).filter(User.id == security_user_id).first()
    logger.info(
        "call.join.security call_session_id=%s security_user_id=%s room_name=%s status=%s",
        row.id,
        security_user_id,
        row.room_name,
        row.status,
    )
    return {
        "callSessionId": row.id,
        "roomName": row.room_name,
        "status": row.status,
        "displayName": (security_user.full_name if security_user else "Security") or "Security",
        "rtcConfig": build_webrtc_rtc_config(),
    }


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
    if row.status in CALL_TERMINAL_STATUSES:
        raise AppException("Call has ended.", status_code=409)
    logger.info(
        "call.join.visitor call_session_id=%s visitor_id=%s room_name=%s status=%s",
        row.id,
        visitor_identity,
        row.room_name,
        row.status,
    )
    return {
        "callSessionId": row.id,
        "roomName": row.room_name,
        "status": row.status,
        "displayName": _get_visitor_display_name(db, row),
        "rtcConfig": build_webrtc_rtc_config(),
    }


async def end_call_session(db: Session, *, call_session_id: str, reason: str | None = None) -> CallSession:
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise AppException("Call session not found.", status_code=404)

    if row.status not in CALL_TERMINAL_STATUSES:
        row.status = "missed" if row.status in CALL_SETUP_STATUSES else "ended"
        row.ended_at = row.ended_at or utc_now()
        row.ended_reason = str(reason or "").strip() or ("unanswered" if row.status == "missed" else "completed")
        db.commit()
        db.refresh(row)
    logger.info(
        "call.ended call_session_id=%s appointment_id=%s visitor_session_id=%s room_name=%s",
        row.id,
        row.appointment_id,
        row.visitor_session_id,
        row.room_name,
    )

    return row
