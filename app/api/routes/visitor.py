import logging
from time import perf_counter

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.session import get_db
from app.schemas.visitor import VisitorRequestCreate
from app.services.appointment_service import (
    accept_appointment_share,
    report_appointment_arrival,
    resolve_appointment_share_token,
    resolve_qr_appointment_token_for_request,
    mark_appointment_qr_used,
)
from app.services.livekit_service import issue_livekit_token
from app.services.notification_service import create_notification
from app.services.qr_service import resolve_qr
from app.services.session_service import create_visitor_session
from app.socket.server import sio

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


class VisitorLiveKitTokenPayload(BaseModel):
    displayName: str | None = None


class VisitorAppointmentAcceptPayload(BaseModel):
    shareToken: str
    deviceId: str
    visitorName: str | None = None


class VisitorAppointmentArrivalPayload(BaseModel):
    shareToken: str
    deviceId: str
    lat: float | None = None
    lng: float | None = None
    batteryPct: int | None = None


@router.post("/request")
async def visitor_request(payload: VisitorRequestCreate, db: Session = Depends(get_db)):
    started = perf_counter()
    phase = "resolve_qr"
    session = None
    try:
        appointment = None
        if str(payload.qrId or "").startswith("qt1."):
            resolved_appointment_qr = resolve_qr_appointment_token_for_request(
                db,
                qr_token=payload.qrId,
                device_id=payload.deviceId,
            )
            appointment = resolved_appointment_qr.get("appointment")
            qr = {
                "home_id": resolved_appointment_qr.get("homeId"),
                "doors": [resolved_appointment_qr.get("doorId")] if resolved_appointment_qr.get("doorId") else [],
                "mode": "direct",
            }
        else:
            qr = resolve_qr(db, payload.qrId)
        phase = "create_session"
        effective_visitor_name = (payload.name or "").strip()
        if not effective_visitor_name and appointment:
            effective_visitor_name = appointment.visitor_name or "Visitor"
        if not effective_visitor_name:
            effective_visitor_name = "Visitor"
        session = create_visitor_session(
            db=db,
            qr_id=payload.qrId,
            qr_home_id=qr["home_id"],
            doors=qr["doors"],
            mode=qr["mode"],
            requested_door=payload.doorId,
            visitor_label=effective_visitor_name,
            appointment_id=appointment.id if appointment else None,
        )
        if appointment:
            mark_appointment_qr_used(db, appointment=appointment, device_id=payload.deviceId)

        phase = "create_notification"
        create_notification(
            db=db,
            user_id=session.homeowner_id,
            kind="visitor.request",
            payload={
                "sessionId": session.id,
                "doorId": session.door_id,
                "visitorName": (payload.name or "Visitor").strip() or "Visitor",
                "purpose": (payload.purpose or "").strip(),
                "message": f"New visitor request from {(payload.name or 'Visitor').strip() or 'Visitor'}",
            },
        )

        phase = "emit_dashboard_patch"
        await sio.emit(
            "dashboard.patch",
            {
                "data": {
                    "activity": [
                        {
                            "id": session.id,
                            "event": f"Visitor request at door {session.door_id}",
                            "time": session.started_at.isoformat(),
                            "state": "pending",
                        }
                    ]
                }
            },
            namespace=settings.DASHBOARD_NAMESPACE,
        )

        elapsed_ms = (perf_counter() - started) * 1000
        logger.info(
            "visitor.request completed in %.1fms phase=%s qr_id=%s session_id=%s",
            elapsed_ms,
            phase,
            payload.qrId,
            session.id,
        )
        return {"data": {"sessionId": session.id, "status": session.status}}
    except Exception:
        elapsed_ms = (perf_counter() - started) * 1000
        logger.exception(
            "visitor.request failed in %.1fms phase=%s qr_id=%s door_id=%s",
            elapsed_ms,
            phase,
            payload.qrId,
            payload.doorId,
        )
        raise


@router.get("/appointments/resolve/{share_token}")
def visitor_resolve_appointment_share(share_token: str, db: Session = Depends(get_db)):
    appt = resolve_appointment_share_token(db, share_token)
    return {
        "data": {
            "id": appt.id,
            "visitorName": appt.visitor_name,
            "purpose": appt.purpose,
            "status": appt.status,
            "startsAt": appt.starts_at.isoformat() if appt.starts_at else None,
            "endsAt": appt.ends_at.isoformat() if appt.ends_at else None,
            "doorId": appt.door_id,
            "homeId": appt.home_id,
            "geofenceLat": appt.geofence_lat,
            "geofenceLng": appt.geofence_lng,
            "geofenceRadiusMeters": int(appt.geofence_radius_m or 0),
        }
    }


@router.post("/appointments/{appointment_id}/accept")
def visitor_accept_appointment(
    appointment_id: str,
    payload: VisitorAppointmentAcceptPayload,
    db: Session = Depends(get_db),
):
    data = accept_appointment_share(
        db,
        share_token=payload.shareToken,
        device_id=payload.deviceId,
        visitor_name=payload.visitorName,
    )
    if data.get("appointment", {}).get("id") != appointment_id:
        raise AppException("Appointment mismatch.", status_code=400)
    return {"data": data}


@router.post("/appointments/{appointment_id}/arrival")
async def visitor_appointment_arrival(
    appointment_id: str,
    payload: VisitorAppointmentArrivalPayload,
    db: Session = Depends(get_db),
):
    data = report_appointment_arrival(
        db,
        appointment_id=appointment_id,
        share_token=payload.shareToken,
        device_id=payload.deviceId,
        lat=payload.lat,
        lng=payload.lng,
        battery_pct=payload.batteryPct,
    )
    await sio.emit(
        "dashboard.patch",
        {
            "data": {
                "activity": [
                    {
                        "id": data.get("id"),
                        "event": f"{data.get('visitorName') or 'Visitor'} entered geofence",
                        "time": data.get("arrivedAt") or "",
                        "state": "arrived",
                    }
                ]
            }
        },
        namespace=settings.DASHBOARD_NAMESPACE,
    )
    return {"data": data}


@router.get("/sessions/{session_id}")
def visitor_session_status(session_id: str, db: Session = Depends(get_db)):
    from app.db.models import VisitorSession
    row = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not row:
        return {"data": {"sessionId": session_id, "status": "not_found"}}
    return {
        "data": {
            "sessionId": row.id,
            "status": row.status,
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "endedAt": row.ended_at.isoformat() if row.ended_at else None,
        }
    }


@router.get("/sessions/{session_id}/messages")
def visitor_session_messages(session_id: str, db: Session = Depends(get_db)):
    from app.db.models import Message, VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        return {"data": []}

    rows = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return {
        "data": [
            {
                "id": row.id,
                "sessionId": row.session_id,
                "text": row.body,
                "senderType": row.sender_type,
                "displayName": "Homeowner" if row.sender_type == "homeowner" else "Visitor",
                "at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.post("/sessions/{session_id}/livekit-token")
def visitor_livekit_token(
    session_id: str,
    payload: VisitorLiveKitTokenPayload,
    db: Session = Depends(get_db),
):
    from app.db.models import VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        raise AppException("Session not found", status_code=404)
    if session.status in {"closed", "rejected"}:
        raise AppException("Session is not available for calls.", status_code=400)

    display_name = (payload.displayName or session.visitor_label or "Visitor").strip() or "Visitor"
    data = issue_livekit_token(
        session_id=session_id,
        identity=f"visitor:{session_id}",
        display_name=display_name,
        can_publish=True,
        can_subscribe=True,
    )
    return {"data": data}
