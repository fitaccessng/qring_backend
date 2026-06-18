from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.orm import Session
import logging
from typing import Optional
import uuid

from app.api.deps import get_optional_current_user
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import Appointment, CallSession, User, VisitorSession
from app.db.session import get_db
from app.services.call_service import (
    end_call_session,
    join_call_as_homeowner,
    join_call_as_security,
    join_call_as_visitor,
    start_call_session,
)
from app.services.realtime_config_service import build_webrtc_rtc_config
from app.services.realtime_notification_service import (
    build_notification_envelope,
    build_notification_idempotency_key,
    emit_dashboard_notification,
    emit_signaling_notification,
)
from app.services.security_service import resolve_security_call_target
from app.socket.server import sio

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


class StartCallPayload(BaseModel):
    appointmentId: Optional[str] = None
    sessionId: Optional[str] = None
    visitorId: Optional[str] = None
    visitorName: Optional[str] = None
    hasVideo: Optional[bool] = None
    type: Optional[str] = None
    visitorToken: Optional[str] = None
    communicationTarget: Optional[str] = None

    @model_validator(mode="after")
    def validate_target(self):
        if not (self.appointmentId or self.sessionId):
            raise ValueError("appointmentId or sessionId is required")
        return self

    @field_validator("appointmentId", "sessionId", mode="before")
    @classmethod
    def validate_uuid_fields(cls, value):
        if value in (None, ""):
            return None
        try:
            return str(uuid.UUID(str(value)))
        except Exception as exc:
            raise ValueError("must be a valid UUID") from exc

    @field_validator("visitorName", mode="before")
    @classmethod
    def validate_visitor_name(cls, value):
        if value in (None, ""):
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) > 120:
            raise ValueError("visitorName must be 120 characters or less")
        return text


class JoinCallPayload(BaseModel):
    callSessionId: str
    participantType: str
    visitorId: Optional[str] = None
    visitorToken: Optional[str] = None

    @field_validator("callSessionId", mode="before")
    @classmethod
    def validate_call_session_id(cls, value):
        try:
            return str(uuid.UUID(str(value)))
        except Exception as exc:
            raise ValueError("callSessionId must be a valid UUID") from exc


class EndCallPayload(BaseModel):
    callSessionId: str
    participantType: Optional[str] = None
    visitorId: Optional[str] = None
    visitorToken: Optional[str] = None

    @field_validator("callSessionId", mode="before")
    @classmethod
    def validate_call_session_id(cls, value):
        try:
            return str(uuid.UUID(str(value)))
        except Exception as exc:
            raise ValueError("callSessionId must be a valid UUID") from exc


def _caller_origin_label(role: str | None) -> str:
    normalized_role = str(role or "").strip().lower()
    if normalized_role == "security":
        return "security dashboard"
    if normalized_role == "homeowner":
        return "homeowner dashboard"
    return "approved-session screen"


@router.post("/start")
async def start_call(
    payload: StartCallPayload,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_optional_current_user),
):
    logger.info(
        "api.call.start request appointment_id=%s session_id=%s user_id=%s user_role=%s has_homeowner_auth=%s",
        payload.appointmentId,
        payload.sessionId,
        user.id if user else None,
        user.role.value if user else "visitor",
        bool(user and user.role.value == "homeowner"),
    )
    if not user:
        if not payload.sessionId:
            raise AppException("sessionId is required for visitor call start.", status_code=400)
        session = db.query(VisitorSession).filter(VisitorSession.id == payload.sessionId).first()
        if not session:
            raise AppException("Visitor session not found.", status_code=404)
        from app.services.visitor_session_auth import require_visitor_session_access

        require_visitor_session_access(db, session=session, visitor_token=payload.visitorToken)
    visitor_session = db.query(VisitorSession).filter(VisitorSession.id == payload.sessionId).first() if payload.sessionId else None
    appointment = db.query(Appointment).filter(Appointment.id == payload.appointmentId).first() if payload.appointmentId else None
    if payload.appointmentId and not appointment:
        raise AppException("Appointment not found.", status_code=404)

    normalized_target = (payload.communicationTarget or "").strip().lower() or None
    if normalized_target not in {None, "visitor", "gateman"}:
        raise AppException("communicationTarget must be visitor or gateman.", status_code=400)
    if normalized_target is None and visitor_session:
        normalized_target = (visitor_session.preferred_communication_target or "").strip().lower() or None

    security_user = None
    if normalized_target == "gateman":
        if user and user.role.value == "homeowner":
            security_user = resolve_security_call_target(db, visitor_session=visitor_session, appointment=appointment)
            if not security_user:
                raise AppException("No security user is available for this estate.", status_code=404)
        else:
            normalized_target = None
    try:
        homeowner_id = user.id if user and user.role.value == "homeowner" else None
        receiver_id = None
        if user and user.role.value == "security":
            receiver_id = appointment.homeowner_id if appointment else (visitor_session.homeowner_id if visitor_session else None)
        elif security_user:
            receiver_id = security_user.id
        elif appointment:
            receiver_id = appointment.homeowner_id
        elif visitor_session:
            receiver_id = visitor_session.homeowner_id
        row = await start_call_session(
            db,
            appointment_id=payload.appointmentId,
            visitor_session_id=payload.sessionId,
            visitor_id=payload.visitorId,
            homeowner_id=homeowner_id,
            security_user_id=security_user.id if security_user else (user.id if user and user.role.value == "security" else None),
            caller_id=user.id if user else None,
            receiver_id=receiver_id,
            call_type=payload.type or ("video" if payload.hasVideo else "audio"),
            visitor_name=payload.visitorName,
        )
    except AppException:
        raise
    except Exception as exc:
        logger.exception(
            "api.call.start.unhandled_error session_id=%s appointment_id=%s user_id=%s call_type=%s",
            payload.sessionId,
            payload.appointmentId,
            user.id if user else None,
            payload.type or ("video" if payload.hasVideo else "audio"),
        )
        raise AppException("Unable to start call session right now.", status_code=500) from exc

    linked_session = None
    if payload.sessionId:
        linked_session = payload.sessionId
    elif row.visitor_session_id:
        linked_session = row.visitor_session_id
    else:
        visit = (
            db.query(VisitorSession)
            .filter(VisitorSession.appointment_id == row.appointment_id)
            .order_by(VisitorSession.started_at.desc())
            .first()
        )
        linked_session = visit.id if visit else None

    incoming_room_user_id = row.security_user_id if (user and user.role.value == "homeowner" and row.security_user_id) else row.homeowner_id
    caller_name = (user.full_name if user else "") or (payload.visitorName or row.visitor_id or "Visitor")
    caller_role = row.initiated_by_role or (user.role.value if user else "visitor")
    caller_origin = _caller_origin_label(caller_role)
    homeowner_name = ""
    if row.homeowner_id:
        homeowner = db.query(User).filter(User.id == row.homeowner_id).first()
        homeowner_name = (homeowner.full_name if homeowner else "") or ""

    if linked_session:
        call_invite_key = build_notification_idempotency_key(
            event_type="call.invite",
            user_id=incoming_room_user_id,
            session_id=linked_session,
            entity_id=row.id,
            action=row.status,
        )
        event_payload = build_notification_envelope(
            notification_id=row.id,
            event_type="call.invite",
            idempotency_key=call_invite_key,
            session_id=linked_session,
            user_id=user.id if user else None,
            source="calls.start",
            payload={
                "eventId": row.id,
                "sessionId": linked_session,
                "callSessionId": row.id,
                "appointmentId": row.appointment_id,
                "roomName": row.room_name,
                "deliveryRoom": f"session:{linked_session}",
                "status": row.status,
                "visitorId": row.visitor_id,
                "hasVideo": bool(payload.hasVideo),
                "type": row.call_type,
                "role": user.role.value if user else "visitor",
                "callerName": caller_name,
                "callerRole": caller_role,
                "callerOrigin": caller_origin,
                "homeownerName": homeowner_name,
                "receiverId": incoming_room_user_id,
                "receiverRole": "security" if incoming_room_user_id == row.security_user_id and row.security_user_id else "homeowner",
            },
        )
        await emit_signaling_notification(
            event_name="call.invite",
            rooms=[f"session:{linked_session}"],
            payload=event_payload,
            idempotency_key=call_invite_key,
            source="calls.start",
        )
        await emit_dashboard_notification(
            event_name="incoming-call",
            rooms=[f"user:{incoming_room_user_id}"],
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="incoming-call",
                idempotency_key=f"dashboard:{call_invite_key}",
                session_id=linked_session,
                user_id=incoming_room_user_id,
                source="calls.start",
                payload={
                    "eventId": row.id,
                    "sessionId": linked_session,
                    "callSessionId": row.id,
                    "appointmentId": row.appointment_id,
                    "roomName": row.room_name,
                    "deliveryRoom": f"session:{linked_session}",
                    "status": row.status,
                    "visitorId": row.visitor_id,
                    "hasVideo": bool(payload.hasVideo),
                    "type": row.call_type,
                    "role": user.role.value if user else "visitor",
                    "callerName": caller_name,
                    "callerRole": caller_role,
                    "callerOrigin": caller_origin,
                    "homeownerName": homeowner_name,
                    "receiverId": incoming_room_user_id,
                    "receiverRole": "security" if incoming_room_user_id == row.security_user_id and row.security_user_id else "homeowner",
                    "message": f"{caller_name} is calling from the {caller_origin}.",
                },
            ),
            idempotency_key=f"dashboard:{call_invite_key}",
            source="calls.start",
        )

    return {
        "data": {
            "status": "ok",
            "roomName": row.room_name,
            "callSessionId": row.id,
            "visitorId": row.visitor_id,
            "callStatus": row.status,
            "state": "connecting",
            "rtcConfig": build_webrtc_rtc_config(),
        }
    }


@router.post("/join")
async def join_call(
    payload: JoinCallPayload,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_optional_current_user),
):
    logger.info(
        "api.call.join request call_session_id=%s participant_type=%s homeowner_auth=%s",
        payload.callSessionId,
        payload.participantType,
        bool(user and user.role.value == "homeowner"),
    )
    participant_type = (payload.participantType or "").strip().lower()
    if participant_type not in {"homeowner", "visitor", "security"}:
        raise AppException("participantType must be homeowner, visitor or security.", status_code=400)

    if participant_type == "homeowner":
        if not user or user.role.value != "homeowner":
            raise AppException("Homeowner authentication is required.", status_code=401)
        data = join_call_as_homeowner(db, call_session_id=payload.callSessionId, homeowner_id=user.id)
    elif participant_type == "security":
        if not user or user.role.value != "security":
            raise AppException("Security authentication is required.", status_code=401)
        data = join_call_as_security(db, call_session_id=payload.callSessionId, security_user_id=user.id)
    else:
        # Visitor join must be tied to the visitor session token as well (prevents IDOR via leaked callSessionId).
        if not (payload.visitorId or "").strip():
            raise AppException("visitorId is required for visitor join requests.", status_code=400)
        target_call = db.query(CallSession).filter(CallSession.id == payload.callSessionId).first()
        if not target_call:
            raise AppException("Call session not found.", status_code=404)
        session_id = target_call.visitor_session_id or target_call.visitor_id
        if not session_id:
            raise AppException("Visitor session context is missing for this call.", status_code=400)
        session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
        if not session:
            raise AppException("Visitor session not found.", status_code=404)
        from app.services.visitor_session_auth import require_visitor_session_access

        require_visitor_session_access(db, session=session, visitor_token=payload.visitorToken)
        data = join_call_as_visitor(
            db,
            call_session_id=payload.callSessionId,
            visitor_id=(payload.visitorId or ""),
        )

    return {
        "data": {
            "callSessionId": data["callSessionId"],
            "roomName": data["roomName"],
            "status": data["status"],
            "displayName": data.get("displayName"),
            "rtcConfig": data.get("rtcConfig"),
        }
    }


@router.post("/end")
async def end_call(
    payload: EndCallPayload,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_optional_current_user),
):
    logger.info(
        "api.call.end request call_session_id=%s participant_type=%s homeowner_auth=%s",
        payload.callSessionId,
        payload.participantType,
        bool(user and user.role.value == "homeowner"),
    )
    participant_type = (payload.participantType or "").strip().lower()
    if participant_type and participant_type not in {"homeowner", "visitor", "security"}:
        raise AppException("participantType must be homeowner, visitor or security.", status_code=400)

    if participant_type == "homeowner" and (not user or user.role.value != "homeowner"):
        raise AppException("Homeowner authentication is required.", status_code=401)
    if participant_type == "security" and (not user or user.role.value != "security"):
        raise AppException("Security authentication is required.", status_code=401)
    if participant_type == "visitor" and not (payload.visitorId or "").strip():
        raise AppException("visitorId is required for visitor end requests.", status_code=400)
    if participant_type == "visitor":
        target_call = db.query(CallSession).filter(CallSession.id == payload.callSessionId).first()
        if not target_call:
            raise AppException("Call session not found.", status_code=404)
        session_id = target_call.visitor_session_id or target_call.visitor_id
        if not session_id:
            raise AppException("Visitor session context is missing for this call.", status_code=400)
        session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
        if not session:
            raise AppException("Visitor session not found.", status_code=404)
        from app.services.visitor_session_auth import require_visitor_session_access

        require_visitor_session_access(db, session=session, visitor_token=payload.visitorToken)

    target = db.query(CallSession).filter(CallSession.id == payload.callSessionId).first()
    if not target:
        raise AppException("Call session not found.", status_code=404)
    if participant_type == "homeowner" and target.homeowner_id != user.id:
        raise AppException("You are not allowed to end this call.", status_code=403)
    if participant_type == "security" and target.security_user_id != user.id:
        raise AppException("You are not allowed to end this call.", status_code=403)
    if participant_type == "visitor" and target.visitor_id != (payload.visitorId or "").strip():
        raise AppException("You are not allowed to end this call.", status_code=403)
    if not participant_type:
        if user and user.role.value == "homeowner":
            if target.homeowner_id != user.id:
                raise AppException("You are not allowed to end this call.", status_code=403)
        elif (payload.visitorId or "").strip():
            if target.visitor_id != (payload.visitorId or "").strip():
                raise AppException("You are not allowed to end this call.", status_code=403)
        else:
            raise AppException("Authorization is required to end call.", status_code=401)

    row = await end_call_session(db, call_session_id=payload.callSessionId)

    session_rows = (
        db.query(VisitorSession.id)
        .filter(VisitorSession.appointment_id == row.appointment_id)
        .all()
        if row.appointment_id
        else []
    )
    session_ids = [r[0] for r in session_rows if r and r[0]]
    if row.visitor_session_id and row.visitor_session_id not in session_ids:
        session_ids.append(row.visitor_session_id)
    if row.visitor_id and row.visitor_id not in session_ids:
        session_ids.append(row.visitor_id)

    event_payload = build_notification_envelope(
        notification_id=row.id,
        event_type="call.ended",
        idempotency_key=build_notification_idempotency_key(
            event_type="call.ended",
            user_id=row.homeowner_id,
            session_id=session_ids[0] if session_ids else row.visitor_session_id,
            entity_id=row.id,
            action=row.status,
        ),
        session_id=session_ids[0] if session_ids else None,
        user_id=user.id if user else None,
        source="calls.end",
        payload={
            "eventId": row.id,
            "sessionId": session_ids[0] if session_ids else None,
            "callSessionId": row.id,
            "appointmentId": row.appointment_id,
            "visitorId": row.visitor_id,
            "roomName": row.room_name,
            "status": row.status,
            "endedAt": row.ended_at.isoformat() if row.ended_at else None,
            "role": user.role.value if user else (participant_type or "visitor"),
        },
    )

    target_rooms = {f"homeowner:{row.homeowner_id}"}
    target_rooms.update({f"session:{session_id}" for session_id in session_ids})
    await emit_signaling_notification(
        event_name="call.ended",
        rooms=target_rooms,
        payload=event_payload,
        idempotency_key=str(event_payload.get("idempotencyKey") or row.id),
        source="calls.end",
    )

    return {"data": {"callSessionId": row.id, "status": row.status, "endedAt": row.ended_at.isoformat() if row.ended_at else None}}
