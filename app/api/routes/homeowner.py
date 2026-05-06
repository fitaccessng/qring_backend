from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Query, UploadFile
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
from app.services.access_pass_service import (
    create_homeowner_access_pass,
    deactivate_access_pass,
    list_homeowner_access_passes,
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
from app.services.notification_service import mark_session_notifications_read
from app.services.security_service import serialize_security_session, update_security_session_status
from app.services.voice_note_service import save_voice_note
from app.socket.server import sio
from app.core.config import get_settings
from app.services.estate_alert_service import create_homeowner_maintenance_request, attach_alert_payment_proof
from app.services.estate_service import join_estate_by_token

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
    autoApproveTrustedVisitors: bool = False
    autoApproveKnownContacts: bool = False
    knownContacts: list[str] = []
    allowDeliveryDropAtGate: bool = True
    smsFallbackEnabled: bool = False
    nearbyPanicAlertsEnabled: bool = True
    nearbyPanicAlertRadiusMeters: int = 500
    nearbyPanicAvailability: str = "always"
    nearbyPanicCustomSchedule: list[dict] = []
    nearbyPanicReceiveFrom: str = "everyone"
    nearbyPanicMutedUntil: Optional[datetime] = None
    nearbyPanicSameAreaLabel: Optional[str] = None
    panicIdentityVisibility: str = "masked"
    safetyHomeLocation: Optional[dict] = None


class HomeownerProfileUpdate(BaseModel):
    fullName: str
    phone: Optional[str] = None


class JoinEstatePayload(BaseModel):
    joinToken: str
    unitName: str


class VisitDecisionPayload(BaseModel):
    action: str
    communicationChannel: Optional[str] = None
    communicationTarget: Optional[str] = None


class HomeownerMessagePayload(BaseModel):
    text: str
    clientId: Optional[str] = None


class LiveKitTokenPayload(BaseModel):
    displayName: Optional[str] = None


class MaintenanceRequestPayload(BaseModel):
    title: str
    description: str = ""


class AccessPassCreatePayload(BaseModel):
    label: str
    passType: str = "qr"
    visitorName: Optional[str] = None
    doorId: Optional[str] = None
    validForHours: int = 24
    maxUses: int = 1


class AppointmentCreatePayload(BaseModel):
    doorId: str
    visitorName: str
    visitorContact: str = ""
    visitorEmail: Optional[str] = None
    purpose: str = ""
    startsAt: str
    endsAt: str
    geofenceLat: Optional[float] = None
    geofenceLng: Optional[float] = None
    geofenceRadiusMeters: Optional[int] = None


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
        visitor_email=payload.visitorEmail,
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


@router.post("/messages/{session_id}/voice-notes")
async def homeowner_upload_voice_note(
    session_id: str,
    media: UploadFile = File(...),
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
        raise AppException("Session not found", status_code=404)

    data = await media.read()
    payload = save_voice_note(
        media_bytes=data,
        filename_hint=media.filename or "voice-note.webm",
        content_type=media.content_type,
        session_id=session_id,
    )
    return {"data": {"url": payload["url"], "contentType": payload["contentType"]}}


@router.post("/alerts/{alert_id}/payment-proof")
async def homeowner_upload_payment_proof(
    alert_id: str,
    media: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = await media.read()
    payload = attach_alert_payment_proof(
        db,
        alert_id=alert_id,
        homeowner_id=user.id,
        media_bytes=data,
        filename_hint=media.filename or "payment-proof.jpg",
        content_type=media.content_type,
    )
    return {"data": payload}


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
    data = get_homeowner_doors_data(db, homeowner_id=user.id)
    if isinstance(data, dict):
        return {"data": data}
    if isinstance(data, list):
        return {"data": {"doors": data, "subscription": None}}
    return {"data": {"doors": [], "subscription": None}}


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


@router.post("/maintenance-requests")
def homeowner_maintenance_request(
    payload: MaintenanceRequestPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = create_homeowner_maintenance_request(
        db=db,
        homeowner_id=user.id,
        title=payload.title,
        description=payload.description,
    )
    return {"data": data}


@router.get("/settings")
def homeowner_settings(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": get_homeowner_settings_payload(db, user.id)}


@router.get("/contact-users/search")
def homeowner_contact_user_search(
    email: str = Query(..., min_length=3),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        raise AppException("Email is required.", status_code=400)

    matched = (
        db.query(User)
        .filter(
            User.email == normalized_email,
            User.id != user.id,
            User.is_active.is_(True),
            User.email_verified.is_(True),
        )
        .first()
    )
    if not matched:
        raise AppException("No verified QRing user found for that email.", status_code=404)

    return {
        "data": {
            "id": matched.id,
            "fullName": matched.full_name,
            "email": matched.email,
            "phone": matched.phone,
            "role": matched.role.value if hasattr(matched.role, "value") else str(matched.role),
        }
    }


@router.post("/join-estate")
def homeowner_join_estate(
    payload: JoinEstatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = join_estate_by_token(
        db=db,
        homeowner_id=user.id,
        join_token=payload.joinToken,
        unit_name=payload.unitName,
    )
    return {"data": data}


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
        auto_approve_trusted_visitors=payload.autoApproveTrustedVisitors,
        auto_approve_known_contacts=payload.autoApproveKnownContacts,
        known_contacts=payload.knownContacts,
        allow_delivery_drop_at_gate=payload.allowDeliveryDropAtGate,
        sms_fallback_enabled=payload.smsFallbackEnabled,
        nearby_panic_alerts_enabled=payload.nearbyPanicAlertsEnabled,
        nearby_panic_alert_radius_m=payload.nearbyPanicAlertRadiusMeters,
        nearby_panic_availability_mode=payload.nearbyPanicAvailability,
        nearby_panic_schedule=payload.nearbyPanicCustomSchedule,
        nearby_panic_receive_from=payload.nearbyPanicReceiveFrom,
        nearby_panic_muted_until=payload.nearbyPanicMutedUntil,
        nearby_panic_same_area_label=payload.nearbyPanicSameAreaLabel,
        panic_identity_visibility=payload.panicIdentityVisibility,
        safety_home_lat=(payload.safetyHomeLocation or {}).get("lat"),
        safety_home_lng=(payload.safetyHomeLocation or {}).get("lng"),
    )
    return {"data": updated}


@router.put("/profile")
def homeowner_update_profile(
    payload: HomeownerProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    user.full_name = (payload.fullName or "").strip() or user.full_name
    user.phone = (payload.phone or "").strip() or None
    db.commit()
    db.refresh(user)
    return {
        "data": {
            "id": user.id,
            "fullName": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        }
    }


@router.get("/access-passes")
def homeowner_access_passes(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": list_homeowner_access_passes(db, homeowner_id=user.id)}


@router.post("/access-passes")
def homeowner_create_access_pass(
    payload: AccessPassCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = create_homeowner_access_pass(
        db,
        homeowner_id=user.id,
        label=payload.label,
        pass_type=payload.passType,
        visitor_name=payload.visitorName,
        door_id=payload.doorId,
        valid_for_hours=payload.validForHours,
        max_uses=payload.maxUses,
    )
    return {"data": data}


@router.post("/access-passes/{access_pass_id}/deactivate")
def homeowner_deactivate_access_pass(
    access_pass_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": deactivate_access_pass(db, homeowner_id=user.id, access_pass_id=access_pass_id)}


@router.post("/visits/{session_id}/decision")
async def homeowner_visit_decision(
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

    updated = update_security_session_status(
        db,
        session_id=session_id,
        actor=user,
        action=action,
        preferred_communication_channel=payload.communicationChannel,
        preferred_communication_target=payload.communicationTarget,
    )
    if not updated:
        raise AppException("Visit not found", status_code=404)
    await sio.emit(
        "dashboard.patch",
        {
            "data": {
                "activity": [
                    {
                        "id": session.id,
                        "event": f"Visitor request {updated.status}",
                        "time": session.started_at.isoformat() if session.started_at else "",
                        "state": updated.status,
                    }
                ]
            }
        },
        namespace=settings.DASHBOARD_NAMESPACE,
    )
    await sio.emit(
        "session.status",
        {
            "sessionId": session.id,
            "status": updated.status,
            "gateStatus": updated.gate_status,
            "homeownerId": session.homeowner_id,
            "doorId": session.door_id,
            "visitorName": session.visitor_label or "Visitor",
        },
        room=f"session:{session.id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    await sio.emit(
        "visitor_forwarded" if action == "approve" else "gate_action_completed",
        {"data": serialize_security_session(db, updated), "action": action, "actorRole": user.role.value},
        namespace=settings.DASHBOARD_NAMESPACE,
    )
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

    mark_session_notifications_read(
        db,
        user_id=user.id,
        session_id=session_id,
        appointment_id=session.appointment_id,
    )

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
