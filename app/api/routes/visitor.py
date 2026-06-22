from __future__ import annotations

import logging
import base64
import binascii
from time import perf_counter
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header
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
from app.services.qr_service import resolve_qr
from app.services.security_service import notify_security_request, serialize_security_session
from app.services.session_service import create_visitor_session, rotate_visitor_session_token
from app.services.visitor_session_auth import issue_visitor_session_token, require_visitor_session_access
from app.services.advanced_service import create_snapshot_audit
from app.services.homeowner_service import create_visitor_session_message
from app.services.realtime_notification_service import (
    build_notification_envelope,
    build_notification_idempotency_key,
    emit_dashboard_notification,
    emit_signaling_notification,
)
from app.socket.server import sio
from app.core.time import utc_now

router = APIRouter()
canonical_router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)
VISITOR_CONSENT_MAX_AGE_HOURS = max(1, int(getattr(settings, "VISITOR_CONSENT_MAX_AGE_HOURS", 24) or 24))
MAX_VISITOR_SNAPSHOT_BYTES = max(1, int(getattr(settings, "MAX_VISITOR_SNAPSHOT_BYTES", 3 * 1024 * 1024) or 3 * 1024 * 1024))


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


def _serialize_call_session(call_session) -> dict[str, object]:
    if not call_session:
        return {}
    return {
        "callSessionId": call_session.id,
        "visitorSessionId": call_session.visitor_session_id,
        "visitorRequestId": call_session.visitor_request_id,
        "status": call_session.status,
        "callType": call_session.call_type,
        "roomName": call_session.room_name,
        "homeownerId": call_session.homeowner_id,
        "visitorId": call_session.visitor_id,
        "createdAt": call_session.created_at.isoformat() if call_session.created_at else None,
        "endedAt": call_session.ended_at.isoformat() if call_session.ended_at else None,
        "answeredAt": call_session.answered_at.isoformat() if call_session.answered_at else None,
    }


def _serialize_message_row(message_row, *, visitor_label: str) -> dict[str, object]:
    sender_role = "homeowner" if message_row.sender_type == "homeowner" else "visitor"
    return {
        "messageId": message_row.id,
        "id": message_row.id,
        "sessionId": message_row.session_id,
        "text": message_row.body,
        "messageType": "text",
        "snapshotUrl": None,
        "photoUrl": None,
        "senderRole": sender_role,
        "senderType": sender_role,
        "senderId": message_row.sender_id,
        "displayName": "Homeowner" if sender_role == "homeowner" else (visitor_label or "Visitor"),
        "visitorName": visitor_label or "Visitor",
        "timestamp": message_row.created_at.isoformat(),
        "at": message_row.created_at.isoformat(),
        "persisted": True,
    }


def _decode_snapshot_base64(snapshot_b64: str) -> bytes:
    normalized = "".join((snapshot_b64 or "").split())
    if normalized.startswith("data:") and "," in normalized:
        normalized = normalized.split(",", 1)[1]
    if not normalized:
        return b""
    remainder = len(normalized) % 4
    if remainder:
        normalized += "=" * (4 - remainder)
    try:
        return base64.b64decode(normalized, validate=False)
    except (binascii.Error, ValueError):
        return b""


def _resolve_session_messages(db: Session, *, session) -> list[dict[str, object]]:
    from app.db.models import Message

    rows = (
        db.query(Message)
        .filter(Message.session_id == session.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    snapshot_url = str(session.snapshot_url or session.photo_url or "").strip()
    payload: list[dict[str, object]] = []
    if snapshot_url:
        payload.append(
            {
                "messageId": f"snapshot:{session.id}",
                "id": f"snapshot:{session.id}",
                "sessionId": session.id,
                "text": "Visitor snapshot submitted.",
                "messageType": "visitor_snapshot",
                "snapshotUrl": snapshot_url,
                "photoUrl": snapshot_url,
                "senderRole": "visitor",
                "senderType": "visitor",
                "displayName": session.visitor_label or "Visitor",
                "visitorName": session.visitor_label or "Visitor",
                "visitorPhone": session.visitor_phone or "",
                "purpose": session.purpose or "",
                "doorId": session.door_id,
                "timestamp": session.started_at.isoformat() if session.started_at else None,
                "at": session.started_at.isoformat() if session.started_at else None,
                "persisted": True,
            }
        )
    payload.extend(_serialize_message_row(row, visitor_label=session.visitor_label or "Visitor") for row in rows)
    return payload


def _resolve_active_call(db: Session, *, session_id: str):
    from app.db.models import CallSession

    return (
        db.query(CallSession)
        .filter(CallSession.visitor_session_id == session_id)
        .filter(CallSession.status.in_(["ringing", "accepted", "connecting", "connected", "reconnecting"]))
        .order_by(CallSession.created_at.desc())
        .first()
    )


def _resolve_latest_call(db: Session, *, session_id: str):
    from app.db.models import CallSession

    return (
        db.query(CallSession)
        .filter(CallSession.visitor_session_id == session_id)
        .order_by(CallSession.created_at.desc())
        .first()
    )


def _build_home_contact_payload(row) -> dict[str, object]:
    return {
        "id": row.homeowner_id,
        "fullName": None,
    }


def _build_home_payload(session) -> dict[str, object]:
    return {
        "id": session.home_id,
        "name": None,
    }


def _build_door_payload(session) -> dict[str, object]:
    return {
        "id": session.door_id,
        "name": None,
    }


def _validate_visitor_consent(payload: VisitorRequestCreate) -> None:
    consent_accepted = bool(getattr(payload, "consentAccepted", False))
    consent_accepted_at = getattr(payload, "consentAcceptedAt", None)
    consent_storage = getattr(payload, "consentStorage", None)
    if not consent_accepted:
        raise AppException(
            "Visitor consent is required before submitting a request.",
            status_code=400,
            code="VISITOR_CONSENT_REQUIRED",
        )
    if not consent_accepted_at:
        raise AppException(
            "Visitor consent timestamp is required.",
            status_code=400,
            code="VISITOR_CONSENT_TIMESTAMP_REQUIRED",
        )
    accepted_at = consent_accepted_at
    if accepted_at.tzinfo is not None:
        accepted_at = accepted_at.replace(tzinfo=None)
    age_seconds = (utc_now() - accepted_at).total_seconds()
    if age_seconds < 0 or age_seconds > VISITOR_CONSENT_MAX_AGE_HOURS * 3600:
        raise AppException(
            "Visitor consent has expired. Please accept the privacy notice again.",
            status_code=400,
            code="VISITOR_CONSENT_EXPIRED",
        )
    if str(consent_storage or "").strip().lower() not in {"session", "sessionstorage", "local", "localstorage"}:
        raise AppException(
            "Visitor consent storage is invalid.",
            status_code=400,
            code="VISITOR_CONSENT_STORAGE_INVALID",
        )


@router.post("/request")
async def visitor_request(payload: VisitorRequestCreate, db: Session = Depends(get_db)):
    started = perf_counter()
    phase = "resolve_qr"
    session = None
    try:
        _validate_visitor_consent(payload)
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
                visitor_token = issue_visitor_session_token(db, session=existing)
                snapshot_url = str(existing.snapshot_url or existing.photo_url or "").strip() or None
                return {
                    "data": {
                        "sessionId": existing.id,
                        "status": existing.status,
                        "visitorToken": visitor_token,
                        "snapshotUrl": snapshot_url,
                        "photoUrl": snapshot_url,
                    }
                }

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
        missing_fields = [
            field_name
            for field_name, field_value in {
                "visitorName": effective_visitor_name,
                "phoneNumber": payload.phoneNumber,
                "purpose": payload.purpose,
                "snapshotBase64": payload.snapshotBase64,
            }.items()
            if not str(field_value or "").strip()
        ]
        if missing_fields:
            logger.warning(
                "visitor.request missing_fields session_id=%s qr_id=%s missing=%s",
                session.id,
                payload.qrId,
                ",".join(missing_fields),
            )
        phase = "capture_snapshot"
        snapshot_audit = None
        snapshot_b64 = (payload.snapshotBase64 or "").strip()
        snapshot_mime = (payload.snapshotMime or "image/jpeg").strip().lower()
        logger.info(
            "QRING_SNAPSHOT_BACKEND_RECEIVED",
            extra={
                "request_id": request_id,
                "has_snapshot_base64": bool(snapshot_b64),
                "snapshot_base64_length": len(snapshot_b64 or ""),
                "snapshot_mime": snapshot_mime,
            },
        )
        if not snapshot_b64:
            db.delete(session)
            db.commit()
            raise AppException(
                "Snapshot could not be saved. Please retake the photo and try again.",
                status_code=400,
                code="SNAPSHOT_SAVE_FAILED",
            )

        try:
            media_bytes = _decode_snapshot_base64(snapshot_b64)
        except Exception as exc:
            db.delete(session)
            db.commit()
            raise AppException(
                "Snapshot could not be saved. Please retake the photo and try again.",
                status_code=400,
                code="SNAPSHOT_SAVE_FAILED",
            ) from exc

        try:
            if not media_bytes:
                raise AppException(
                    "Snapshot could not be saved. Please retake the photo and try again.",
                    status_code=400,
                    code="SNAPSHOT_SAVE_FAILED",
                )

            if len(media_bytes) > MAX_VISITOR_SNAPSHOT_BYTES:
                raise AppException(
                    "Snapshot could not be saved. Please retake the photo and try again.",
                    status_code=400,
                    code="SNAPSHOT_SAVE_FAILED",
                )

            ext = ".jpg"
            if "png" in snapshot_mime:
                ext = ".png"
            elif "webp" in snapshot_mime:
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
        except AppException:
            db.delete(session)
            db.commit()
            raise
        except Exception as exc:
            db.delete(session)
            db.commit()
            logger.exception("Snapshot capture failed and request was rolled back.")
            raise AppException(
                "Snapshot could not be saved. Please retake the photo and try again.",
                status_code=500,
                code="SNAPSHOT_SAVE_FAILED",
            ) from exc

        if snapshot_audit and isinstance(snapshot_audit, dict):
            try:
                await emit_dashboard_notification(
                    event_name="visitor.snapshot",
                    rooms=[f"user:{session.homeowner_id}"],
                    payload={"data": build_notification_envelope(
                        notification_id=snapshot_audit.get("id"),
                        event_type="visitor.snapshot",
                        idempotency_key=build_notification_idempotency_key(
                            event_type="visitor.snapshot",
                            user_id=session.homeowner_id,
                            session_id=session.id,
                            entity_id=str(snapshot_audit.get("id") or ""),
                        ),
                        session_id=session.id,
                        user_id=session.homeowner_id,
                        source="visitor.request.snapshot",
                        payload=snapshot_audit,
                    )},
                    idempotency_key=build_notification_idempotency_key(
                        event_type="visitor.snapshot",
                        user_id=session.homeowner_id,
                        session_id=session.id,
                        entity_id=str(snapshot_audit.get("id") or ""),
                    ),
                    source="visitor.request.snapshot",
                )
            except Exception:
                logger.exception("Failed to emit visitor.snapshot realtime event")

        if snapshot_audit and isinstance(snapshot_audit, dict):
            snapshot_url = str(snapshot_audit.get("fileUrl") or snapshot_audit.get("url") or "").strip()
            if not snapshot_url:
                db.delete(session)
                db.commit()
                raise AppException(
                    "Snapshot could not be saved. Please retake the photo and try again.",
                    status_code=500,
                    code="SNAPSHOT_SAVE_FAILED",
                )
            session.photo_url = snapshot_url or None
            session.snapshot_url = snapshot_url or None
            db.commit()
            db.refresh(session)
            if appointment:
                mark_appointment_qr_used(db, appointment=appointment, device_id=payload.deviceId)
            logger.info(
                "QRING_SNAPSHOT_BACKEND_SAVED",
                extra={
                    "request_id": request_id,
                    "visitor_session_id": session.id,
                    "snapshot_url": session.snapshot_url,
                },
            )
            logger.info(
                "QRING_SNAPSHOT_DB_COMMITTED",
                extra={
                    "visitor_session_id": session.id,
                    "visitor_request_id": request_id,
                    "snapshot_url": session.snapshot_url,
                    "photo_url": session.photo_url,
                    "metadata": None,
                },
            )
            visitor_token = issue_visitor_session_token(db, session=session)
        else:
            db.delete(session)
            db.commit()
            raise AppException(
                "Snapshot could not be saved. Please retake the photo and try again.",
                status_code=500,
                code="SNAPSHOT_SAVE_FAILED",
            )

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
                "photoUrl": session.snapshot_url or session.photo_url,
                "snapshotUrl": session.snapshot_url or session.photo_url,
                "snapshotAuditId": snapshot_audit.get("id") if isinstance(snapshot_audit, dict) else None,
                "estateId": session.estate_id,
                "requestSource": session.request_source or "visitor_qr",
                "creatorRole": session.creator_role or "visitor",
                "message": f"New visitor request from {session.visitor_label or 'Visitor'}",
            },
            idempotency_key=build_notification_idempotency_key(
                event_type="visitor.request",
                user_id=session.homeowner_id,
                session_id=session.id,
                entity_id=str(request_id or session.id),
                action=session.status,
            ),
            source="visitor.request",
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
        incoming_call_key = build_notification_idempotency_key(
            event_type="incoming-call",
            user_id=session.homeowner_id,
            session_id=session.id,
            entity_id=str(appointment.id if appointment else session.id),
            action="ringing",
        )
        incoming_call_payload = build_notification_envelope(
            event_type="incoming-call",
            idempotency_key=incoming_call_key,
            session_id=session.id,
            user_id=session.homeowner_id,
            source="visitor.request.arrival",
            payload={
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
        )
        await emit_dashboard_notification(
            event_name="incoming-call",
            rooms=[f"user:{session.homeowner_id}"],
            payload=incoming_call_payload,
            idempotency_key=f"dashboard:{incoming_call_key}",
            source="visitor.request.arrival",
        )
        await emit_signaling_notification(
            event_name="incoming-call",
            rooms=[f"homeowner:{session.homeowner_id}"],
            payload=incoming_call_payload,
            idempotency_key=f"signaling:{incoming_call_key}",
            source="visitor.request.arrival",
        )

        elapsed_ms = (perf_counter() - started) * 1000
        logger.info(
            "visitor.request completed in %.1fms phase=%s qr_id=%s session_id=%s",
            elapsed_ms,
            phase,
            payload.qrId,
            session.id,
        )
        return {
            "data": {
                "sessionId": session.id,
                "status": session.status,
                "visitorToken": visitor_token,
                "snapshotUrl": session.snapshot_url or session.photo_url,
                "photoUrl": session.snapshot_url or session.photo_url,
            }
        }
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise AppException(str(exc), status_code=409) from exc
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
    arrival_key = build_notification_idempotency_key(
        event_type="incoming-call",
        user_id=str(data.get("homeownerId") or ""),
        session_id=str(data.get("sessionId") or ""),
        entity_id=str(appointment_id or ""),
        action="arrived",
    )
    arrival_payload = build_notification_envelope(
        event_type="incoming-call",
        idempotency_key=arrival_key,
        session_id=str(data.get("sessionId") or ""),
        user_id=str(data.get("homeownerId") or ""),
        source="visitor.appointment.arrival",
        payload={
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
    )
    await emit_dashboard_notification(
        event_name="incoming-call",
        rooms=[f"user:{data.get('homeownerId')}"],
        payload=arrival_payload,
        idempotency_key=f"dashboard:{arrival_key}",
        source="visitor.appointment.arrival",
    )
    await emit_signaling_notification(
        event_name="incoming-call",
        rooms=[f"homeowner:{data.get('homeownerId')}"],
        payload=arrival_payload,
        idempotency_key=f"signaling:{arrival_key}",
        source="visitor.appointment.arrival",
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
    logger.info("visitor.session_status.request session_id=%s has_user=%s", session_id, bool(user))
    row = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not row:
        logger.warning("visitor.session_status.not_found session_id=%s", session_id)
        raise AppException("Session not found", status_code=404, code="VISITOR_SESSION_NOT_FOUND")

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

    data = serialize_security_session(db, row)
    return {
        "data": {
            "sessionId": row.id,
            "status": row.status,
            "sessionStatus": row.status,
            "snapshotUrl": row.snapshot_url or row.photo_url,
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "endedAt": row.ended_at.isoformat() if row.ended_at else None,
            "sessionRoute": data.get("sessionRoute"),
            "sessionRoomId": data.get("sessionRoomId"),
            "sessionActivated": data.get("sessionActivated"),
            "communicationStatus": data.get("communicationStatus"),
            "preferredCommunicationChannel": data.get("preferredCommunicationChannel"),
            "preferredCommunicationTarget": data.get("preferredCommunicationTarget"),
            "visitor": {
                "fullName": row.visitor_label or "Visitor",
                "phoneNumber": row.visitor_phone or "",
                "purpose": row.purpose or "",
                "photoUrl": row.snapshot_url or row.photo_url,
                "snapshotUrl": row.snapshot_url or row.photo_url,
                "timestamp": row.started_at.isoformat() if row.started_at else None,
            },
            "location": {
                "estateId": data.get("estateId"),
                "estateName": data.get("estateName"),
                "buildingName": data.get("buildingName"),
                "unitName": data.get("unitName"),
                "doorId": data.get("doorId"),
                "doorName": data.get("doorName"),
                "gateLabel": data.get("gateLabel"),
            },
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

    logger.info("visitor.session_messages.request session_id=%s has_user=%s", session_id, bool(user))
    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        logger.warning("visitor.session_messages.not_found session_id=%s", session_id)
        raise AppException("Session not found", status_code=404, code="VISITOR_SESSION_NOT_FOUND")

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
    snapshot_url = session.snapshot_url or session.photo_url
    snapshot_message = None
    if snapshot_url:
        snapshot_message = {
            "messageId": f"snapshot:{session.id}",
            "id": f"snapshot:{session.id}",
            "sessionId": session.id,
            "text": "Visitor snapshot submitted.",
            "messageType": "visitor_snapshot",
            "snapshotUrl": snapshot_url,
            "photoUrl": snapshot_url,
            "senderRole": "visitor",
            "senderType": "visitor",
            "displayName": session.visitor_label or "Visitor",
            "visitorName": session.visitor_label or "Visitor",
            "visitorPhone": session.visitor_phone or "",
            "purpose": session.purpose or "",
            "doorId": session.door_id,
            "timestamp": session.started_at.isoformat() if session.started_at else None,
            "at": session.started_at.isoformat() if session.started_at else None,
            "persisted": True,
        }
    serialized_rows = [
        {
            "messageId": message_row.id,
            "id": message_row.id,
            "sessionId": message_row.session_id,
            "text": message_row.body,
            "messageType": "text",
            "snapshotUrl": None,
            "photoUrl": None,
            "senderRole": "homeowner" if message_row.sender_type == "homeowner" else "visitor",
            "senderType": message_row.sender_type,
            "displayName": "Homeowner" if message_row.sender_type == "homeowner" else "Visitor",
            "timestamp": message_row.created_at.isoformat(),
            "at": message_row.created_at.isoformat(),
        }
        for message_row in rows
    ]
    if snapshot_message and not any(item.get("messageId") == snapshot_message["messageId"] for item in serialized_rows):
        serialized_rows.insert(0, snapshot_message)
    return {
        "data": serialized_rows
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

    logger.info("visitor.session_message.send session_id=%s has_visitor_token=%s", session_id, bool(visitorToken or x_visitor_token))
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
    await sio.emit(
        "chat.read",
        {
            "sessionId": session_id,
            "readerType": "visitor",
            "at": data.get("at"),
        },
        room=f"session:{session_id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": data}


def _authorize_session_access(db: Session, *, session, user, visitor_token: Optional[str]) -> None:
    from app.db.models import Estate, UserRole

    if user is None:
        require_visitor_session_access(db, session=session, visitor_token=visitor_token)
        return

    if user.role == UserRole.admin:
        return
    if user.role == UserRole.homeowner:
        if session.homeowner_id != user.id:
            raise AppException("Not authorized to access this session.", status_code=403)
        return
    if user.role == UserRole.security:
        if not user.estate_id or not session.estate_id or session.estate_id != user.estate_id:
            raise AppException("Not authorized to access this session.", status_code=403)
        return
    if user.role == UserRole.estate:
        if not session.estate_id:
            raise AppException("Not authorized to access this session.", status_code=403)
        estate = db.query(Estate).filter(Estate.id == session.estate_id, Estate.owner_id == user.id).first()
        if not estate:
            raise AppException("Not authorized to access this session.", status_code=403)
        return
    raise AppException("Not authorized to access this session.", status_code=403)


@canonical_router.get("/visitor-sessions/{visitor_session_id}")
def get_visitor_session_contract(
    visitor_session_id: str,
    visitorToken: Optional[str] = None,
    x_visitor_token: Optional[str] = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
    user=Depends(get_optional_current_user),
):
    from app.db.models import Door, Home, User, VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == visitor_session_id).first()
    if not session:
        raise AppException("Session not found", status_code=404, code="VISITOR_SESSION_NOT_FOUND")

    _authorize_session_access(db, session=session, user=user, visitor_token=visitorToken or x_visitor_token)
    active_call = _resolve_active_call(db, session_id=session.id)
    messages = _resolve_session_messages(db, session=session)
    homeowner = db.query(User).filter(User.id == session.homeowner_id).first()
    home = db.query(Home).filter(Home.id == session.home_id).first()
    door = db.query(Door).filter(Door.id == session.door_id).first()

    return {
        "data": {
            "visitorSessionId": session.id,
            "visitorRequestId": session.request_id,
            "status": session.status,
            "snapshotUrl": session.snapshot_url or session.photo_url,
            "visitor": {
                "fullName": session.visitor_label or "Visitor",
                "phoneNumber": session.visitor_phone or "",
                "purpose": session.purpose or "",
                "photoUrl": session.snapshot_url or session.photo_url,
                "snapshotUrl": session.snapshot_url or session.photo_url,
            },
            "messages": messages,
            "activeCall": _serialize_call_session(active_call) if active_call else None,
            "homeowner": {
                "id": homeowner.id if homeowner else session.homeowner_id,
                "fullName": homeowner.full_name if homeowner else None,
                "email": homeowner.email if homeowner else None,
            },
            "home": {
                "id": home.id if home else session.home_id,
                "name": home.name if home else None,
            },
            "door": {
                "id": door.id if door else session.door_id,
                "name": door.name if door else None,
                "gateLabel": door.gate_label if door else None,
            },
        }
    }


@canonical_router.get("/visitor-requests/{visitor_request_id}/thread")
def get_visitor_request_thread_contract(
    visitor_request_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_optional_current_user),
):
    from app.db.models import CallSession, Door, Home, User, VisitorSession

    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.request_id == visitor_request_id)
        .order_by(VisitorSession.started_at.desc())
        .first()
    )
    if not session:
        raise AppException("Visitor request not found", status_code=404, code="VISITOR_REQUEST_NOT_FOUND")

    _authorize_session_access(db, session=session, user=user, visitor_token=None)
    messages = _resolve_session_messages(db, session=session)
    active_call = _resolve_active_call(db, session_id=session.id)
    latest_call = _resolve_latest_call(db, session_id=session.id)
    homeowner = db.query(User).filter(User.id == session.homeowner_id).first()
    home = db.query(Home).filter(Home.id == session.home_id).first()
    door = db.query(Door).filter(Door.id == session.door_id).first()

    return {
        "data": {
            "visitorRequestId": visitor_request_id,
            "visitorSessionId": session.id,
            "status": session.status,
            "snapshotUrl": session.snapshot_url or session.photo_url,
            "visitor": {
                "fullName": session.visitor_label or "Visitor",
                "phoneNumber": session.visitor_phone or "",
                "purpose": session.purpose or "",
                "photoUrl": session.snapshot_url or session.photo_url,
                "snapshotUrl": session.snapshot_url or session.photo_url,
            },
            "messages": messages,
            "latestCall": _serialize_call_session(latest_call) if latest_call else None,
            "activeCall": _serialize_call_session(active_call) if active_call else None,
            "homeowner": {
                "id": homeowner.id if homeowner else session.homeowner_id,
                "fullName": homeowner.full_name if homeowner else None,
            },
            "home": {
                "id": home.id if home else session.home_id,
                "name": home.name if home else None,
            },
            "door": {
                "id": door.id if door else session.door_id,
                "name": door.name if door else None,
                "gateLabel": door.gate_label if door else None,
            },
        }
    }


@canonical_router.get("/visitor-sessions/{visitor_session_id}/active-call")
def get_visitor_session_active_call(
    visitor_session_id: str,
    visitorToken: Optional[str] = None,
    x_visitor_token: Optional[str] = Header(default=None, alias="X-Visitor-Token"),
    db: Session = Depends(get_db),
    user=Depends(get_optional_current_user),
):
    from app.db.models import VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == visitor_session_id).first()
    if not session:
        raise AppException("Session not found", status_code=404, code="VISITOR_SESSION_NOT_FOUND")
    _authorize_session_access(db, session=session, user=user, visitor_token=visitorToken or x_visitor_token)
    active_call = _resolve_active_call(db, session_id=session.id)
    return {"data": _serialize_call_session(active_call) if active_call else None}
