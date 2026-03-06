from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.services.homeowner_settings_service import (
    get_homeowner_settings_payload,
    update_homeowner_settings,
)
from app.services.homeowner_service import (
    create_homeowner_session_message,
    create_homeowner_door,
    delete_homeowner_session_message,
    get_homeowner_context,
    get_homeowner_doors_data,
    generate_homeowner_door_qr,
    list_homeowner_session_messages,
    list_homeowner_message_threads,
    list_homeowner_visits,
)
from app.services.appointment_service import (
    create_appointment,
    create_appointment_share,
    list_homeowner_appointments,
)
from app.services.session_service import mark_session_status
from app.core.exceptions import AppException
from app.services.livekit_service import issue_livekit_token
from app.socket.server import sio
from app.core.config import get_settings

router = APIRouter()
settings = get_settings()


class DoorQrCreate(BaseModel):
    mode: str = "direct"
    plan: str = "single"


class HomeownerDoorCreate(BaseModel):
    name: str
    generateQr: bool = True
    mode: str = "direct"
    plan: str = "single"


class HomeownerSettingsUpdate(BaseModel):
    pushAlerts: bool
    soundAlerts: bool
    autoRejectUnknownVisitors: bool


class VisitDecisionPayload(BaseModel):
    action: str


class HomeownerMessagePayload(BaseModel):
    text: str
    clientId: Optional[str] = None


class LiveKitTokenPayload(BaseModel):
    displayName: Optional[str] = None


class AppointmentCreatePayload(BaseModel):
    doorId: str
    visitorName: str
    visitorContact: str = ""
    purpose: str = ""
    startsAt: str
    endsAt: str
    geofenceLat: float | None = None
    geofenceLng: float | None = None
    geofenceRadiusMeters: int | None = None


@router.get("/visits")
def homeowner_visits(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": list_homeowner_visits(db, homeowner_id=user.id)}


@router.get("/appointments")
def homeowner_appointments(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": list_homeowner_appointments(db, homeowner_id=user.id)}


@router.post("/appointments")
def homeowner_create_appointment(
    payload: AppointmentCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = create_appointment(
        db,
        homeowner_id=user.id,
        door_id=payload.doorId,
        visitor_name=payload.visitorName,
        visitor_contact=payload.visitorContact,
        purpose=payload.purpose,
        starts_at_iso=payload.startsAt,
        ends_at_iso=payload.endsAt,
        geofence_lat=payload.geofenceLat,
        geofence_lng=payload.geofenceLng,
        geofence_radius_meters=payload.geofenceRadiusMeters,
    )
    return {"data": data}


@router.post("/appointments/{appointment_id}/share")
def homeowner_share_appointment(
    appointment_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": create_appointment_share(db, homeowner_id=user.id, appointment_id=appointment_id)}


@router.get("/context")
def homeowner_context(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": get_homeowner_context(db, homeowner_id=user.id)}


@router.get("/messages")
def homeowner_messages(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": list_homeowner_message_threads(db, homeowner_id=user.id)}


@router.get("/messages/{session_id}")
def homeowner_session_messages(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    rows = list_homeowner_session_messages(db, homeowner_id=user.id, session_id=session_id)
    return {"data": rows}


@router.post("/messages/{session_id}")
async def homeowner_send_message(
    session_id: str,
    payload: HomeownerMessagePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = create_homeowner_session_message(
        db,
        homeowner_id=user.id,
        session_id=session_id,
        text=payload.text,
    )
    if not data:
        raise AppException("Unable to send message", status_code=400)
    await sio.emit(
        "chat.message",
        {
            **data,
            "clientId": payload.clientId,
            "displayName": user.full_name or "Homeowner",
        },
        room=f"session:{session_id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": data}


@router.delete("/messages/{session_id}/{message_id}")
def homeowner_delete_message(
    session_id: str,
    message_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    deleted = delete_homeowner_session_message(
        db,
        homeowner_id=user.id,
        session_id=session_id,
        message_id=message_id,
    )
    if not deleted:
        raise AppException("Message not found", status_code=404)
    return {"data": {"id": message_id, "deleted": True}}


@router.get("/doors")
def homeowner_doors(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": get_homeowner_doors_data(db, homeowner_id=user.id)}


@router.post("/doors")
def homeowner_create_door(
    payload: HomeownerDoorCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = create_homeowner_door(
        db=db,
        homeowner_id=user.id,
        name=payload.name,
        generate_qr=payload.generateQr,
        mode=payload.mode,
        qr_plan=payload.plan,
    )
    return {"data": data}


@router.post("/doors/{door_id}/qr")
def homeowner_generate_door_qr(
    door_id: str,
    payload: DoorQrCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = generate_homeowner_door_qr(
        db=db,
        homeowner_id=user.id,
        door_id=door_id,
        mode=payload.mode,
        plan=payload.plan,
    )
    return {"data": data}


@router.get("/settings")
def homeowner_settings(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": get_homeowner_settings_payload(db, user.id)}


@router.put("/settings")
def homeowner_update_settings(
    payload: HomeownerSettingsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    updated = update_homeowner_settings(
        db=db,
        user_id=user.id,
        push_alerts=payload.pushAlerts,
        sound_alerts=payload.soundAlerts,
        auto_reject_unknown_visitors=payload.autoRejectUnknownVisitors,
    )
    return {"data": updated}


@router.post("/visits/{session_id}/decision")
def homeowner_visit_decision(
    session_id: str,
    payload: VisitDecisionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    action = (payload.action or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise AppException("Action must be approve or reject", status_code=400)

    from app.db.models import VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id, VisitorSession.homeowner_id == user.id).first()
    if not session:
        raise AppException("Visit not found", status_code=404)

    updated = mark_session_status(db, session_id=session_id, status="approved" if action == "approve" else "rejected")
    if not updated:
        raise AppException("Visit not found", status_code=404)
    return {"data": {"id": updated.id, "status": updated.status}}


@router.post("/visits/{session_id}/end")
async def homeowner_end_visit(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    from datetime import datetime
    from app.db.models import Appointment, VisitorSession

    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.id == session_id, VisitorSession.homeowner_id == user.id)
        .first()
    )
    if not session:
        raise AppException("Visit not found", status_code=404)

    updated = mark_session_status(db, session_id=session_id, status="closed")
    if not updated:
        raise AppException("Visit not found", status_code=404)

    if session.appointment_id:
        appointment = (
            db.query(Appointment)
            .filter(Appointment.id == session.appointment_id, Appointment.homeowner_id == user.id)
            .first()
        )
        if appointment and appointment.status not in {"completed", "cancelled", "expired"}:
            appointment.status = "completed"
            appointment.qr_token_hash = None
            appointment.qr_payload_encrypted = None
            appointment.qr_signature = None
            appointment.qr_expires_at = datetime.utcnow()
            appointment.share_token_hash = None
            db.query(VisitorSession).filter(
                VisitorSession.appointment_id == appointment.id,
                VisitorSession.status.in_(["pending", "approved", "active"]),
            ).update(
                {
                    VisitorSession.status: "closed",
                    VisitorSession.ended_at: datetime.utcnow(),
                },
                synchronize_session=False,
            )
            db.commit()

    await sio.emit(
        "session.control",
        {"sessionId": session_id, "action": "end"},
        room=f"session:{session_id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": {"id": updated.id, "status": updated.status}}


@router.post("/visits/{session_id}/livekit-token")
def homeowner_livekit_token(
    session_id: str,
    payload: LiveKitTokenPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    from app.db.models import VisitorSession

    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.id == session_id, VisitorSession.homeowner_id == user.id)
        .first()
    )
    if not session:
        raise AppException("Visit not found", status_code=404)
    if session.status in {"closed", "rejected"}:
        raise AppException("Session is not available for calls.", status_code=400)

    display_name = (payload.displayName or user.full_name or "Homeowner").strip() or "Homeowner"
    data = issue_livekit_token(
        session_id=session_id,
        identity=f"homeowner:{user.id}",
        display_name=display_name,
        can_publish=True,
        can_subscribe=True,
    )
    return {"data": data}

