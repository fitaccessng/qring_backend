from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.orm import Session
import logging
import uuid

from app.api.deps import get_optional_current_user
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import CallSession, User, VisitorSession
from app.db.session import get_db
from app.services.call_service import (
    end_call_session,
    join_call_as_homeowner,
    join_call_as_visitor,
    start_call_session,
)
from app.socket.server import sio

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


class StartCallPayload(BaseModel):
    appointmentId: str | None = None
    sessionId: str | None = None
    visitorId: str | None = None
    visitorName: str | None = None

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
    visitorId: str | None = None

    @field_validator("callSessionId", mode="before")
    @classmethod
    def validate_call_session_id(cls, value):
        try:
            return str(uuid.UUID(str(value)))
        except Exception as exc:
            raise ValueError("callSessionId must be a valid UUID") from exc


class EndCallPayload(BaseModel):
    callSessionId: str
    participantType: str | None = None
    visitorId: str | None = None

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
    user: User | None = Depends(get_optional_current_user),
):
    logger.info(
        "api.call.start request appointment_id=%s session_id=%s has_homeowner_auth=%s",
        payload.appointmentId,
        payload.sessionId,
        bool(user and user.role.value == "homeowner"),
    )
    try:
        row = await start_call_session(
            db,
            appointment_id=payload.appointmentId,
            visitor_session_id=payload.sessionId,
            visitor_id=payload.visitorId,
            homeowner_id=user.id if user and user.role.value == "homeowner" else None,
            visitor_name=payload.visitorName,
        )
    except AppException:
        raise
    except Exception as exc:
        logger.exception("api.call.start.unhandled_error session_id=%s appointment_id=%s", payload.sessionId, payload.appointmentId)
        raise AppException(f"Unable to start call session: {exc}", status_code=500) from exc

    linked_session = None
    if payload.sessionId:
        linked_session = payload.sessionId
    else:
        visit = (
            db.query(VisitorSession)
            .filter(VisitorSession.appointment_id == row.appointment_id)
            .order_by(VisitorSession.started_at.desc())
            .first()
        )
        linked_session = visit.id if visit else None

    await sio.emit(
        "call.invite",
        {
            "sessionId": linked_session,
            "callSessionId": row.id,
            "appointmentId": row.appointment_id,
            "roomName": row.room_name,
            "status": row.status,
            "visitorId": row.visitor_id,
        },
        room=f"homeowner:{row.homeowner_id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )

    return {
        "data": {
            "roomName": row.room_name,
            "callSessionId": row.id,
            "visitorId": row.visitor_id,
            "status": "ringing",
        }
    }


@router.post("/join")
async def join_call(
    payload: JoinCallPayload,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
):
    logger.info(
        "api.call.join request call_session_id=%s participant_type=%s homeowner_auth=%s",
        payload.callSessionId,
        payload.participantType,
        bool(user and user.role.value == "homeowner"),
    )
    participant_type = (payload.participantType or "").strip().lower()
    if participant_type not in {"homeowner", "visitor"}:
        raise AppException("participantType must be homeowner or visitor.", status_code=400)

    if participant_type == "homeowner":
        if not user or user.role.value != "homeowner":
            raise AppException("Homeowner authentication is required.", status_code=401)
        data = join_call_as_homeowner(db, call_session_id=payload.callSessionId, homeowner_id=user.id)
    else:
        data = join_call_as_visitor(
            db,
            call_session_id=payload.callSessionId,
            visitor_id=(payload.visitorId or ""),
        )

    return {"data": {"token": data["token"], "roomName": data["roomName"], "status": data["status"]}}


@router.post("/end")
async def end_call(
    payload: EndCallPayload,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
):
    logger.info(
        "api.call.end request call_session_id=%s participant_type=%s homeowner_auth=%s",
        payload.callSessionId,
        payload.participantType,
        bool(user and user.role.value == "homeowner"),
    )
    participant_type = (payload.participantType or "").strip().lower()
    if participant_type and participant_type not in {"homeowner", "visitor"}:
        raise AppException("participantType must be homeowner or visitor.", status_code=400)

    if participant_type == "homeowner" and (not user or user.role.value != "homeowner"):
        raise AppException("Homeowner authentication is required.", status_code=401)
    if participant_type == "visitor" and not (payload.visitorId or "").strip():
        raise AppException("visitorId is required for visitor end requests.", status_code=400)

    target = db.query(CallSession).filter(CallSession.id == payload.callSessionId).first()
    if not target:
        raise AppException("Call session not found.", status_code=404)
    if participant_type == "homeowner" and target.homeowner_id != user.id:
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
    )
    session_ids = [r[0] for r in session_rows if r and r[0]]
    if row.visitor_id and row.visitor_id not in session_ids:
        session_ids.append(row.visitor_id)

    event_payload = {
        "sessionId": session_ids[0] if session_ids else None,
        "callSessionId": row.id,
        "appointmentId": row.appointment_id,
        "visitorId": row.visitor_id,
        "roomName": row.room_name,
        "status": row.status,
    }

    target_rooms = {f"homeowner:{row.homeowner_id}"}
    target_rooms.update({f"session:{session_id}" for session_id in session_ids})
    for room_name in target_rooms:
        await sio.emit(
            "call.ended",
            event_payload,
            room=room_name,
            namespace=settings.SIGNALING_NAMESPACE,
        )

    return {"data": {"callSessionId": row.id, "status": row.status, "endedAt": row.ended_at.isoformat() if row.ended_at else None}}
