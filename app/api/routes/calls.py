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
from app.db.models import CallSession, User, VisitorSession
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
    emit_signaling_notification,
)
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
    try:
        row = await start_call_session(
            db,
            appointment_id=payload.appointmentId,
            visitor_session_id=payload.sessionId,
            visitor_id=payload.visitorId,
            homeowner_id=user.id if user and user.role.value == "homeowner" else None,
            security_user_id=user.id if user and user.role.value == "security" else None,
            caller_id=user.id if user else None,
            receiver_id=None,
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

    if linked_session:
        call_invite_key = build_notification_idempotency_key(
            event_type="call.invite",
            user_id=row.homeowner_id,
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
            },
        )
        await emit_signaling_notification(
            event_name="call.invite",
            rooms=[f"session:{linked_session}"],
            payload=event_payload,
            idempotency_key=call_invite_key,
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
