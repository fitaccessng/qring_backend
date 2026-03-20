import uuid
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import Appointment, Door, Home, VisitorSession
from app.services.payment_service import require_subscription_feature
from app.services.notification_service import create_notification
from app.services.qr_token_service import (
    build_qr_token_payload,
    build_secure_token,
    build_share_token_payload,
    decode_secure_token,
    hash_token,
)

settings = get_settings()


def _distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math

    def _to_rad(value: float) -> float:
        return (value * math.pi) / 180.0

    earth_radius_m = 6_371_000
    d_lat = _to_rad(lat2 - lat1)
    d_lng = _to_rad(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(_to_rad(lat1)) * math.cos(_to_rad(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * earth_radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _share_signing_key() -> bytes:
    secret = (settings.QR_TOKEN_SIGNING_KEY or "").strip() or settings.JWT_SECRET_KEY
    return secret.encode("utf-8")


def _build_short_share_token(appointment_id: str, share_token_hash: str) -> str:
    raw = f"{appointment_id}:{share_token_hash}".encode("utf-8")
    signature = hmac.new(_share_signing_key(), raw, hashlib.sha256).hexdigest()[:20]
    return f"asl.{appointment_id}.{signature}"


def _is_valid_short_share_token(token: str, appointment_id: str, share_token_hash: str) -> bool:
    expected = _build_short_share_token(appointment_id, share_token_hash)
    return hmac.compare_digest(str(token or ""), expected)


def _to_dt(value: str) -> datetime:
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            # Persist UTC datetimes as naive values for current DB schema compatibility.
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError as exc:
        raise AppException("Invalid datetime format. Use ISO format.", status_code=400) from exc


def _status_label(status: str) -> str:
    status = str(status or "").strip().lower()
    if status == "created":
        return "Created"
    if status == "shared":
        return "Shared"
    if status == "accepted":
        return "Accepted"
    if status == "arrived":
        return "Arrived"
    if status == "active":
        return "Active"
    if status == "completed":
        return "Completed"
    if status == "expired":
        return "Expired"
    if status == "cancelled":
        return "Cancelled"
    return status.title() or "Created"


def _serialize_appointment(row: Appointment) -> dict[str, Any]:
    return {
        "id": row.id,
        "homeownerId": row.homeowner_id,
        "homeId": row.home_id,
        "doorId": row.door_id,
        "visitorName": row.visitor_name,
        "visitorContact": row.visitor_contact,
        "purpose": row.purpose,
        "startsAt": row.starts_at.isoformat() if row.starts_at else None,
        "endsAt": row.ends_at.isoformat() if row.ends_at else None,
        "status": row.status,
        "statusLabel": _status_label(row.status),
        "geofenceLat": row.geofence_lat,
        "geofenceLng": row.geofence_lng,
        "geofenceRadiusMeters": int(row.geofence_radius_m or 0),
        "acceptedAt": row.accepted_at.isoformat() if row.accepted_at else None,
        "acceptedDeviceId": row.accepted_device_id,
        "shareTokenCreatedAt": row.share_token_created_at.isoformat() if row.share_token_created_at else None,
        "qrExpiresAt": row.qr_expires_at.isoformat() if row.qr_expires_at else None,
        "qrUsedAt": row.qr_used_at.isoformat() if row.qr_used_at else None,
        "arrivedAt": row.arrived_at.isoformat() if row.arrived_at else None,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def ensure_appointment_visitor_session(
    db: Session,
    *,
    appointment: Appointment,
    visitor_label: str | None = None,
    status: str = "pending",
) -> VisitorSession:
    open_statuses = {"pending", "active", "approved"}
    existing = (
        db.query(VisitorSession)
        .filter(
            VisitorSession.appointment_id == appointment.id,
            VisitorSession.homeowner_id == appointment.homeowner_id,
            VisitorSession.status.in_(open_statuses),
        )
        .order_by(VisitorSession.started_at.desc())
        .first()
    )
    if existing:
        updated = False
        desired_label = (visitor_label or appointment.visitor_name or "Visitor").strip() or "Visitor"
        if desired_label and existing.visitor_label != desired_label:
            existing.visitor_label = desired_label
            updated = True
        if status and existing.status not in {"rejected", "closed", "completed"} and existing.status != status:
            existing.status = status
            updated = True
        if updated:
            db.commit()
            db.refresh(existing)
        return existing

    session = VisitorSession(
        qr_id=f"appointment:{appointment.id}",
        home_id=appointment.home_id,
        door_id=appointment.door_id,
        homeowner_id=appointment.homeowner_id,
        appointment_id=appointment.id,
        visitor_label=(visitor_label or appointment.visitor_name or "Visitor").strip() or "Visitor",
        status=status or "pending",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def list_homeowner_appointments(db: Session, homeowner_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = (
        db.query(Appointment)
        .filter(Appointment.homeowner_id == homeowner_id)
        .order_by(Appointment.starts_at.desc(), Appointment.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_serialize_appointment(row) for row in rows]


def create_appointment(
    db: Session,
    *,
    homeowner_id: str,
    door_id: str,
    visitor_name: str,
    visitor_contact: str,
    purpose: str,
    starts_at_iso: str,
    ends_at_iso: str,
    geofence_lat: float | None,
    geofence_lng: float | None,
    geofence_radius_meters: int | None,
) -> dict[str, Any]:
    require_subscription_feature(db, homeowner_id, "visitor_scheduling", user_role="homeowner")
    row = (
        db.query(Door, Home)
        .join(Home, Home.id == Door.home_id)
        .filter(Door.id == door_id, Home.homeowner_id == homeowner_id)
        .first()
    )
    if not row:
        raise AppException("Door not found for homeowner.", status_code=404)
    door, home = row
    starts_at = _to_dt(starts_at_iso)
    ends_at = _to_dt(ends_at_iso)
    if ends_at <= starts_at:
        raise AppException("Appointment end time must be after start time.", status_code=400)
    if ends_at <= datetime.utcnow():
        raise AppException("Appointment must end in the future.", status_code=400)

    radius = int(geofence_radius_meters or settings.APPOINTMENT_DEFAULT_GEOFENCE_RADIUS_METERS)
    radius = max(30, min(radius, 2000))

    appt = Appointment(
        homeowner_id=homeowner_id,
        home_id=home.id,
        door_id=door.id,
        visitor_name=(visitor_name or "Visitor").strip() or "Visitor",
        visitor_contact=(visitor_contact or "").strip(),
        purpose=(purpose or "").strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        status="created",
        geofence_lat=geofence_lat,
        geofence_lng=geofence_lng,
        geofence_radius_m=radius,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return _serialize_appointment(appt)


def create_appointment_share(
    db: Session,
    *,
    homeowner_id: str,
    appointment_id: str,
) -> dict[str, Any]:
    require_subscription_feature(db, homeowner_id, "visitor_scheduling", user_role="homeowner")
    appt = (
        db.query(Appointment)
        .filter(Appointment.id == appointment_id, Appointment.homeowner_id == homeowner_id)
        .first()
    )
    if not appt:
        raise AppException("Appointment not found.", status_code=404)
    if appt.status in {"cancelled", "completed", "expired"}:
        raise AppException("Appointment cannot be shared in current state.", status_code=400)

    expires_at = min(appt.ends_at + timedelta(hours=3), datetime.utcnow() + timedelta(days=3))
    token_data = build_secure_token(
        "as1",
        build_share_token_payload(appointment_id=appt.id, homeowner_id=appt.homeowner_id),
        expires_at=expires_at,
    )
    appt.share_token_hash = token_data["tokenHash"]
    appt.share_token_created_at = datetime.utcnow()
    if appt.status == "created":
        appt.status = "shared"
    db.commit()
    db.refresh(appt)

    base = (settings.APPOINTMENT_SHARE_BASE_URL or settings.FRONTEND_BASE_URL or "").rstrip("/")
    short_share_token = _build_short_share_token(appt.id, token_data["tokenHash"])
    share_url = f"{base}/appointment/{short_share_token}"
    return {
        "appointment": _serialize_appointment(appt),
        "shareToken": short_share_token,
        "shareUrl": share_url,
        "expiresAt": token_data["expiresAt"],
    }


def resolve_appointment_share_token(db: Session, share_token: str) -> Appointment:
    token_value = str(share_token or "").strip()
    if token_value.startswith("asl."):
        parts = token_value.split(".")
        if len(parts) != 3:
            raise AppException("Invalid share token format.", status_code=400)
        _, appointment_id, _ = parts
        appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if not appt:
            raise AppException("Appointment not found.", status_code=404)
        if not appt.share_token_hash:
            raise AppException("Share token is no longer valid.", status_code=400)
        if not _is_valid_short_share_token(token_value, appt.id, appt.share_token_hash):
            raise AppException("Share token is no longer valid.", status_code=400)
        if appt.status in {"cancelled", "completed", "expired"}:
            raise AppException("Appointment is not active.", status_code=400)
        if appt.ends_at <= datetime.utcnow():
            appt.status = "expired"
            db.commit()
            raise AppException("Appointment has expired.", status_code=400)
        return appt

    payload = decode_secure_token(share_token, "as1")
    if payload.get("scope") != "appointment-share":
        raise AppException("Invalid share token scope.", status_code=400)
    appointment_id = str(payload.get("appointmentId") or "").strip()
    if not appointment_id:
        raise AppException("Share token missing appointment id.", status_code=400)
    appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appt:
        raise AppException("Appointment not found.", status_code=404)
    if not appt.share_token_hash or appt.share_token_hash != hash_token(share_token):
        raise AppException("Share token is no longer valid.", status_code=400)
    if appt.status in {"cancelled", "completed", "expired"}:
        raise AppException("Appointment is not active.", status_code=400)
    if appt.ends_at <= datetime.utcnow():
        appt.status = "expired"
        db.commit()
        raise AppException("Appointment has expired.", status_code=400)
    return appt


def accept_appointment_share(
    db: Session,
    *,
    share_token: str,
    device_id: str,
    visitor_name: str | None,
) -> dict[str, Any]:
    appt = resolve_appointment_share_token(db, share_token)
    device = str(device_id or "").strip() or f"visitor-{uuid.uuid4().hex[:8]}"
    visitor_id = f"visitor-{uuid.uuid4().hex[:12]}"
    effective_name = (visitor_name or appt.visitor_name or "Visitor").strip() or "Visitor"

    qr_expiry = min(appt.ends_at, datetime.utcnow() + timedelta(hours=3))
    qr_payload = build_qr_token_payload(
        visitor_id=visitor_id,
        homeowner_id=appt.homeowner_id,
        appointment_id=appt.id,
        device_id=device,
        starts_at=appt.starts_at,
        ends_at=appt.ends_at,
    )
    qr_token_data = build_secure_token("qt1", qr_payload, expires_at=qr_expiry)

    appt.status = "accepted"
    appt.accepted_at = datetime.utcnow()
    appt.accepted_device_id = device
    appt.visitor_name = effective_name
    appt.qr_token_hash = qr_token_data["tokenHash"]
    appt.qr_payload_encrypted = qr_token_data["cipherText"]
    appt.qr_signature = qr_token_data["signature"]
    appt.qr_expires_at = qr_expiry
    appt.qr_used_at = None
    appt.qr_used_device_id = None
    db.commit()
    db.refresh(appt)

    session = ensure_appointment_visitor_session(
        db,
        appointment=appt,
        visitor_label=effective_name,
        status="pending",
    )

    create_notification(
        db=db,
        user_id=appt.homeowner_id,
        kind="appointment.accepted",
        payload={
            "appointmentId": appt.id,
            "doorId": appt.door_id,
            "visitorName": effective_name,
            "message": f"{effective_name} accepted appointment and is expected soon.",
        },
    )

    share_base = (settings.APPOINTMENT_SHARE_BASE_URL or settings.FRONTEND_BASE_URL or "").rstrip("/")
    return {
        "appointment": _serialize_appointment(appt),
        "sessionId": session.id,
        "scanQrToken": qr_token_data["token"],
        "scanUrl": f"{share_base}/scan/{qr_token_data['token']}",
        "geofence": {
            "lat": appt.geofence_lat,
            "lng": appt.geofence_lng,
            "radiusMeters": int(appt.geofence_radius_m or 0),
        },
    }


def resolve_qr_appointment_token_for_request(
    db: Session,
    *,
    qr_token: str,
    device_id: str | None,
) -> dict[str, Any]:
    payload = decode_secure_token(qr_token, "qt1")
    appointment_id = str(payload.get("appointmentId") or "").strip()
    if not appointment_id:
        raise AppException("QR token is missing appointment reference.", status_code=400)
    appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appt:
        raise AppException("Appointment not found.", status_code=404)
    if not appt.qr_token_hash or appt.qr_token_hash != hash_token(qr_token):
        raise AppException("QR token is no longer valid.", status_code=400)
    if appt.qr_used_at:
        raise AppException("This QR token has already been used.", status_code=400)
    if appt.qr_expires_at and appt.qr_expires_at <= datetime.utcnow():
        appt.status = "expired"
        db.commit()
        raise AppException("QR token expired.", status_code=400)
    if appt.status not in {"accepted", "arrived"}:
        raise AppException("Appointment must be accepted before QR usage.", status_code=400)

    payload_device = str(payload.get("deviceId") or "").strip()
    incoming_device = str(device_id or "").strip()
    if payload_device and incoming_device and payload_device != incoming_device:
        raise AppException("QR token device mismatch.", status_code=403)
    if appt.accepted_device_id and incoming_device and appt.accepted_device_id != incoming_device:
        raise AppException("Device is not authorized for this appointment.", status_code=403)

    door = db.query(Door).filter(Door.id == appt.door_id).first()
    if not door:
        raise AppException("Appointment door was not found.", status_code=404)
    home = db.query(Home).filter(Home.id == appt.home_id).first()
    if not home:
        raise AppException("Appointment home was not found.", status_code=404)

    return {
        "appointment": appt,
        "visitorName": appt.visitor_name or "Visitor",
        "homeId": appt.home_id,
        "doorId": appt.door_id,
        "homeownerId": appt.homeowner_id,
        "doorOptions": [
            {
                "id": appt.door_id,
                "name": door.name,
                "homeId": home.id,
                "homeName": home.name,
                "homeownerId": home.homeowner_id,
                "homeownerName": "",
            }
        ],
    }


def mark_appointment_qr_used(
    db: Session,
    *,
    appointment: Appointment,
    device_id: str | None,
) -> None:
    appointment.qr_used_at = datetime.utcnow()
    appointment.qr_used_device_id = str(device_id or "").strip() or appointment.qr_used_device_id
    appointment.status = "active"
    db.commit()


def report_appointment_arrival(
    db: Session,
    *,
    appointment_id: str,
    share_token: str,
    device_id: str,
    lat: float | None,
    lng: float | None,
    battery_pct: int | None,
) -> dict[str, Any]:
    appt = resolve_appointment_share_token(db, share_token)
    if appt.id != appointment_id:
        raise AppException("Share token does not match appointment.", status_code=400)
    if appt.accepted_device_id and appt.accepted_device_id != str(device_id or "").strip():
        raise AppException("Arrival signal device mismatch.", status_code=403)
    if appt.geofence_lat is None or appt.geofence_lng is None:
        raise AppException("Appointment geofence is not configured.", status_code=400)
    if lat is None or lng is None:
        raise AppException("Arrival coordinates are required.", status_code=400)
    radius_m = int(appt.geofence_radius_m or settings.APPOINTMENT_DEFAULT_GEOFENCE_RADIUS_METERS)
    radius_m = max(30, min(radius_m, 2000))
    distance_m = _distance_meters(float(lat), float(lng), float(appt.geofence_lat), float(appt.geofence_lng))
    if distance_m > radius_m:
        raise AppException(
            f"Visitor is outside geofence ({int(distance_m)}m > {radius_m}m).",
            status_code=400,
        )
    appt.arrived_at = datetime.utcnow()
    appt.arrival_lat = lat
    appt.arrival_lng = lng
    appt.arrival_battery_pct = battery_pct
    if appt.status in {"accepted", "shared", "created"}:
        appt.status = "arrived"
    db.commit()
    db.refresh(appt)

    session = ensure_appointment_visitor_session(
        db,
        appointment=appt,
        visitor_label=appt.visitor_name,
        status="pending",
    )

    create_notification(
        db=db,
        user_id=appt.homeowner_id,
        kind="appointment.arrival",
        payload={
            "appointmentId": appt.id,
            "doorId": appt.door_id,
            "visitorName": appt.visitor_name,
            "message": f"{appt.visitor_name or 'Visitor'} entered the geofence and is arriving.",
        },
    )

    data = _serialize_appointment(appt)
    data["sessionId"] = session.id
    data["visitorId"] = session.id
    return data
