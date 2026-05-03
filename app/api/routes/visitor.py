from __future__ import annotations

import logging
import base64
from time import perf_counter
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.api.deps import get_optional_current_user
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
from app.services.qr_service import resolve_qr
from app.services.security_service import notify_security_request, serialize_security_session
from app.services.session_service import create_visitor_session, rotate_visitor_session_token
from app.services.visitor_session_auth import require_visitor_session_access
from app.services.voice_note_service import save_voice_note
from app.services.advanced_service import create_snapshot_audit
from app.services.homeowner_service import create_visitor_session_message
from app.socket.server import sio

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


class VisitorLiveKitTokenPayload(BaseModel):
    displayName: Optional[str] = None


class VisitorAppointmentAcceptPayload(BaseModel):
    shareToken: str
    deviceId: str
    visitorName: Optional[str] = None


class VisitorAppointmentArrivalPayload(BaseModel):
    shareToken: str
    deviceId: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    batteryPct: Optional[int] = None


class VisitorSessionMessagePayload(BaseModel):
    text: str
    clientId: Optional[str] = None


@router.post("/request")
async def visitor_request(payload: VisitorRequestCreate, db: Session = Depends(get_db)):
    started = perf_counter()
    phase = "resolve_qr"
    session = None
    try:
        request_id = str(payload.requestId or "").strip() or None
        if request_id:
            from app.db.models import VisitorSession

            existing = (
                db.query(VisitorSession)
                .filter(VisitorSession.request_id == request_id, VisitorSession.qr_id == payload.qrId)
                .order_by(VisitorSession.started_at.desc())
                .first()
            )
            if existing:
                logger.info(
                    "visitor.request idempotent replay qr_id=%s request_id=%s session_id=%s status=%s",
                    payload.qrId,
                    request_id,
                    existing.id,
                    existing.status,
                )
                return {"data": {"sessionId": existing.id, "status": existing.status}}

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
            request_id=request_id,
            visitor_phone=payload.phoneNumber,
            purpose=payload.purpose,
            visitor_type=payload.visitorType,
            delivery_option=payload.deliveryOption,
            request_source="visitor_qr",
            creator_role="visitor",
            )
        if appointment:
            mark_appointment_qr_used(db, appointment=appointment, device_id=payload.deviceId)

        phase = "capture_snapshot"
        snapshot_audit = None
        snapshot_b64 = (payload.snapshotBase64 or "").strip()
        if snapshot_b64:
            try:
                media_bytes = base64.b64decode(snapshot_b64, validate=True)
            except Exception:
                media_bytes = b""
            if media_bytes:
                try:
                    if len(media_bytes) > 2 * 1024 * 1024:
                        raise AppException("Snapshot is too large. Please retake the photo.", status_code=400)

                    mime = (payload.snapshotMime or "image/jpeg").strip().lower()
                    ext = ".jpg"
                    if "png" in mime:
                        ext = ".png"
                    elif "webp" in mime:
                        ext = ".webp"

                    snapshot_audit = create_snapshot_audit(
                        db,
                        homeowner_id=session.homeowner_id,
                        media_bytes=media_bytes,
                        filename_hint=f"visitor-snapshot{ext}",
                        media_type="photo",
                        visitor_session_id=session.id,
                        appointment_id=appointment.id if appointment else None,
                        source="visitor_qr_scan",
                    )
                    try:
                        await sio.emit(
                            "visitor.snapshot",
                            {"data": snapshot_audit},
                            room=f"user:{session.homeowner_id}",
                            namespace=settings.DASHBOARD_NAMESPACE,
                        )
                    except Exception:
                        logger.exception("Failed to emit visitor.snapshot realtime event")
                except AppException:
                    raise
                except Exception:
                    # Never block a visitor request just because snapshot storage failed.
                    logger.exception("Snapshot capture failed. Continuing without snapshot.")
                    snapshot_audit = None

        if snapshot_audit and isinstance(snapshot_audit, dict):
            session.photo_url = str(snapshot_audit.get("fileUrl") or snapshot_audit.get("url") or "").strip() or None
            db.commit()
            db.refresh(session)

        phase = "create_notification"
        from app.services.notification_service import create_notification

        create_notification(
            db=db,
            user_id=session.homeowner_id,
            kind="visitor.request",
            payload={
                "sessionId": session.id,
                "doorId": session.door_id,
                "visitorName": session.visitor_label or "Visitor",
                "phoneNumber": session.visitor_phone or "",
                "purpose": session.purpose or "",
                "photoUrl": session.photo_url,
                "snapshotAuditId": snapshot_audit.get("id") if isinstance(snapshot_audit, dict) else None,
                "estateId": session.estate_id,
                "requestSource": session.request_source or "visitor_qr",
                "creatorRole": session.creator_role or "visitor",
                "message": f"New visitor request from {session.visitor_label or 'Visitor'}",
            },
        )
        notify_security_request(db, session)

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
                            "state": session.status,
                        }
                    ]
                }
            },
            namespace=settings.DASHBOARD_NAMESPACE,
        )
        await sio.emit(
            "new_visitor_request",
            {"data": serialize_security_session(db, session)},
            namespace=settings.DASHBOARD_NAMESPACE,
        )
        await sio.emit(
            "incoming-call",
            {
                "sessionId": session.id,
                "callSessionId": "",
                "appointmentId": appointment.id if appointment else None,
                "homeownerId": session.homeowner_id,
                "visitorId": session.id,
                "visitorName": effective_visitor_name,
                "doorId": session.door_id,
                "hasVideo": False,
                "state": "ringing",
                "message": f"{effective_visitor_name} arrived at your gate.",
            },
            room=f"user:{session.homeowner_id}",
            namespace=settings.DASHBOARD_NAMESPACE,
        )
        await sio.emit(
            "incoming-call",
            {
                "sessionId": session.id,
                "callSessionId": "",
                "appointmentId": appointment.id if appointment else None,
                "homeownerId": session.homeowner_id,
                "visitorId": session.id,
                "visitorName": effective_visitor_name,
                "doorId": session.door_id,
                "hasVideo": False,
                "state": "ringing",
            },
            room=f"homeowner:{session.homeowner_id}",
            namespace=settings.SIGNALING_NAMESPACE,
        )

        elapsed_ms = (perf_counter() - started) * 1000
        logger.info(
            "visitor.request completed in %.1fms phase=%s qr_id=%s session_id=%s",
            elapsed_ms,
            phase,
            payload.qrId,
            session.id,
        )
        visitor_token = rotate_visitor_session_token(db, session=session)
        return {"data": {"sessionId": session.id, "status": session.status, "visitorToken": visitor_token}}
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
    session_id = str(data.get("sessionId") or "").strip()
    if session_id:
        from app.db.models import VisitorSession

        session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
        if session:
            data["visitorToken"] = rotate_visitor_session_token(db, session=session)
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
    await sio.emit(
        "incoming-call",
        {
            "sessionId": data.get("sessionId"),
            "callSessionId": "",
            "appointmentId": appointment_id,
            "homeownerId": data.get("homeownerId"),
            "visitorId": data.get("visitorId") or data.get("sessionId"),
            "visitorName": data.get("visitorName") or "Visitor",
            "doorId": data.get("doorId"),
            "hasVideo": False,
            "state": "ringing",
            "message": f"{data.get('visitorName') or 'Visitor'} arrived for appointment.",
        },
        room=f"user:{data.get('homeownerId')}",
        namespace=settings.DASHBOARD_NAMESPACE,
    )
    await sio.emit(
        "incoming-call",
        {
            "sessionId": data.get("sessionId"),
            "callSessionId": "",
            "appointmentId": appointment_id,
            "homeownerId": data.get("homeownerId"),
            "visitorId": data.get("visitorId") or data.get("sessionId"),
            "visitorName": data.get("visitorName") or "Visitor",
            "doorId": data.get("doorId"),
            "hasVideo": False,
            "state": "ringing",
            "message": f"{data.get('visitorName') or 'Visitor'} arrived for appointment.",
        },
        room=f"homeowner:{data.get('homeownerId')}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": data}


@router.get("/sessions/{session_id}")
def visitor_session_status(
    session_id: str,
    visitorToken: Optional[str] = None,
    x_visitor_token: Optional[str] = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
    user=Depends(get_optional_current_user),
):
    from app.db.models import VisitorSession
    row = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not row:
        return {"data": {"sessionId": session_id, "status": "not_found"}}

    # AuthZ: homeowner/security/estate/admin via JWT, otherwise require visitor token.
    if user is None:
        require_visitor_session_access(db, session=row, visitor_token=visitorToken or x_visitor_token)
    else:
        from app.db.models import Estate, UserRole

        if user.role == UserRole.admin:
            pass
        elif user.role == UserRole.homeowner:
            if row.homeowner_id != user.id:
                raise AppException("Not authorized to access this session.", status_code=403)
        elif user.role == UserRole.security:
            if not user.estate_id or not row.estate_id or row.estate_id != user.estate_id:
                raise AppException("Not authorized to access this session.", status_code=403)
        elif user.role == UserRole.estate:
            if not row.estate_id:
                raise AppException("Not authorized to access this session.", status_code=403)
            estate = db.query(Estate).filter(Estate.id == row.estate_id, Estate.owner_id == user.id).first()
            if not estate:
                raise AppException("Not authorized to access this session.", status_code=403)
        else:
            raise AppException("Not authorized to access this session.", status_code=403)

    return {
        "data": {
            "sessionId": row.id,
            "status": row.status,
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "endedAt": row.ended_at.isoformat() if row.ended_at else None,
        }
    }


@router.get("/sessions/{session_id}/messages")
def visitor_session_messages(
    session_id: str,
    visitorToken: Optional[str] = None,
    x_visitor_token: Optional[str] = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
    user=Depends(get_optional_current_user),
):
    from app.db.models import Message, VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        return {"data": []}

    if user is None:
        require_visitor_session_access(db, session=session, visitor_token=visitorToken or x_visitor_token)
    else:
        from app.db.models import Estate, UserRole

        if user.role == UserRole.admin:
            pass
        elif user.role == UserRole.homeowner:
            if session.homeowner_id != user.id:
                raise AppException("Not authorized to access this session.", status_code=403)
        elif user.role == UserRole.security:
            if not user.estate_id or not session.estate_id or session.estate_id != user.estate_id:
                raise AppException("Not authorized to access this session.", status_code=403)
        elif user.role == UserRole.estate:
            if not session.estate_id:
                raise AppException("Not authorized to access this session.", status_code=403)
            estate = db.query(Estate).filter(Estate.id == session.estate_id, Estate.owner_id == user.id).first()
            if not estate:
                raise AppException("Not authorized to access this session.", status_code=403)
        else:
            raise AppException("Not authorized to access this session.", status_code=403)

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


@router.post("/sessions/{session_id}/messages")
async def visitor_send_session_message(
    session_id: str,
    payload: VisitorSessionMessagePayload,
    visitorToken: Optional[str] = None,
    x_visitor_token: Optional[str] = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
):
    from app.db.models import VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        raise AppException("Session not found", status_code=404)

    require_visitor_session_access(db, session=session, visitor_token=visitorToken or x_visitor_token)
    data = create_visitor_session_message(db, session_id=session_id, text=payload.text)
    if not data:
        raise AppException("Unable to send message", status_code=400)

    await sio.emit(
        "chat.message",
        {
            **data,
            "clientId": payload.clientId,
            "displayName": session.visitor_label or "Visitor",
        },
        room=f"session:{session_id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": data}


@router.post("/sessions/{session_id}/livekit-token")
def visitor_livekit_token(
    session_id: str,
    payload: VisitorLiveKitTokenPayload,
    visitorToken: Optional[str] = None,
    x_visitor_token: Optional[str] = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
    user=Depends(get_optional_current_user),
):
    from app.db.models import VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        raise AppException("Session not found", status_code=404)
    if session.status in {"closed", "rejected"}:
        raise AppException("Session is not available for calls.", status_code=400)
    if user is None:
        require_visitor_session_access(db, session=session, visitor_token=visitorToken or x_visitor_token)

    display_name = (payload.displayName or session.visitor_label or "Visitor").strip() or "Visitor"
    data = issue_livekit_token(
        session_id=session_id,
        identity=f"visitor:{session_id}",
        display_name=display_name,
        can_publish=True,
        can_subscribe=True,
    )
    return {"data": data}


@router.post("/sessions/{session_id}/voice-notes")
async def visitor_upload_voice_note(
    session_id: str,
    visitorToken: Optional[str] = None,
    x_visitor_token: Optional[str] = Header(default=None, alias="X-Visitor-Token"),
    media: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_optional_current_user),
):
    from app.db.models import VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        raise AppException("Session not found", status_code=404)
    if user is None:
        require_visitor_session_access(db, session=session, visitor_token=visitorToken or x_visitor_token)

    data = await media.read()
    payload = save_voice_note(
        media_bytes=data,
        filename_hint=media.filename or "voice-note.webm",
        content_type=media.content_type,
        session_id=session_id,
    )
    return {"data": {"url": payload["url"], "contentType": payload["contentType"]}}
