from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable

from anyio import from_thread
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.time import utc_now
from app.db.base import Base
from app.db.session import SessionLocal
from app.db.models import (
    AlertDeliveryStatus,
    AuditLog,
    EmergencyAlert,
    EmergencyAlertEvent,
    EmergencyAlertPriority,
    EmergencyAlertStatus,
    EmergencyAlertType,
    Estate,
    Home,
    HomeownerSetting,
    PanicEvent,
    PanicEventStatus,
    PanicMode,
    User,
    UserRole,
    VisitorReport,
    VisitorReportSeverity,
    VisitorReportStatus,
    VisitorSession,
    WatchlistEntry,
    WatchlistRiskLevel,
)
from app.services.notification_service import create_notification
from app.services.provider_integrations import send_email_smtp
from app.socket.server import sio

settings = get_settings()
MAX_VISITOR_REPORTS_PER_DAY = 5
PANIC_RETRY_INTERVAL_SECONDS = 5
PANIC_MAX_RETRIES = 3
PANIC_MAX_TRIGGERS_PER_HOUR = 3
DEFAULT_NEARBY_PANIC_RADIUS_M = 500

PRIORITY_BY_TYPE = {
    EmergencyAlertType.panic: EmergencyAlertPriority.critical,
    EmergencyAlertType.fire: EmergencyAlertPriority.critical,
    EmergencyAlertType.break_in: EmergencyAlertPriority.high,
}

SECURITY_MESSAGE_BY_TYPE = {
    EmergencyAlertType.panic: "Immediate distress alert. Treat as potential threat or kidnapping scenario.",
    EmergencyAlertType.fire: "Fire alert. Coordinate nearest responders and evacuation support.",
    EmergencyAlertType.break_in: "Break-in alert. Dispatch guards and secure access points.",
}


def create_safety_tables(bind) -> None:
    for table in [
        "emergency_alerts",
        "emergency_alert_events",
        "panic_events",
        "visitor_reports",
        "watchlist_entries",
    ]:
        Base.metadata.tables[table].create(bind=bind, checkfirst=True)


def _to_float(value: Decimal | float | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _json_dumps(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=True)


def _json_loads(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _normalize_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _normalize_phone(value: str | None) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits[-11:] if digits else ""


def _normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _to_rad(value: float) -> float:
    return value * math.pi / 180.0


def _distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    earth_radius_m = 6371000.0
    d_lat = _to_rad(lat2 - lat1)
    d_lng = _to_rad(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(_to_rad(lat1)) * math.cos(_to_rad(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(a))


def _parse_recipient_user_ids(raw: str | None) -> list[str]:
    try:
        rows = json.loads(raw or "[]")
    except Exception:
        rows = []
    if not isinstance(rows, list):
        return []
    return [str(item).strip() for item in rows if str(item).strip()]


def _parse_json_list(raw: str | None) -> list[dict[str, Any]]:
    try:
        rows = json.loads(raw or "[]")
    except Exception:
        rows = []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _dedupe_users(rows: list[User]) -> list[User]:
    unique_rows: list[User] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not row or row.id in seen_ids:
            continue
        seen_ids.add(row.id)
        unique_rows.append(row)
    return unique_rows


def _panic_trigger_trust_score(user: User) -> int:
    score = 10
    if bool(getattr(user, "email_verified", False)):
        score += 45
    if bool(getattr(user, "phone", None)):
        score += 10
    if bool(getattr(user, "estate_id", None)):
        score += 15
    return min(score, 100)


def _blur_location_label(address: str | None, unit_label: str | None) -> str:
    if unit_label:
        return f"Near {unit_label}"
    safe_address = str(address or "").strip()
    if not safe_address:
        return "Nearby"
    parts = [part.strip() for part in safe_address.split(",") if part.strip()]
    return f"Near {parts[0]}" if parts else "Nearby"


def _bucket_distance(distance_m: float | None) -> int | None:
    if distance_m is None or not math.isfinite(distance_m):
        return None
    if distance_m <= 150:
        return 120
    if distance_m <= 350:
        return 300
    if distance_m <= 750:
        return 500
    return int(round(distance_m / 1000.0, 1) * 1000)


def _is_night_hour(now: datetime) -> bool:
    current = now.time()
    return current >= time(20, 0) or current < time(6, 0)


def _schedule_matches(now: datetime, schedule_rows: list[dict[str, Any]]) -> bool:
    if not schedule_rows:
        return True
    weekday = now.weekday()
    current_minutes = now.hour * 60 + now.minute
    for row in schedule_rows:
        days = row.get("days")
        if isinstance(days, list) and days and weekday not in {int(day) for day in days if str(day).isdigit()}:
            continue
        start = str(row.get("start") or "").strip()
        end = str(row.get("end") or "").strip()
        if not start or not end:
            continue
        try:
            start_hour, start_minute = [int(part) for part in start.split(":", 1)]
            end_hour, end_minute = [int(part) for part in end.split(":", 1)]
        except Exception:
            continue
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        if start_minutes <= end_minutes and start_minutes <= current_minutes <= end_minutes:
            return True
        if start_minutes > end_minutes and (current_minutes >= start_minutes or current_minutes <= end_minutes):
            return True
    return False


def _recipient_allows_panic(sender: User, recipient: User, recipient_settings: HomeownerSetting | None, *, now: datetime) -> bool:
    if not recipient_settings:
        return False
    if not bool(getattr(recipient_settings, "nearby_panic_alerts_enabled", True)):
        return False
    muted_until = getattr(recipient_settings, "nearby_panic_muted_until", None)
    if muted_until and muted_until > now:
        return False

    availability = str(getattr(recipient_settings, "nearby_panic_availability_mode", "always") or "always").lower()
    if availability == "night_only" and not _is_night_hour(now):
        return False
    if availability == "custom" and not _schedule_matches(now, _parse_json_list(getattr(recipient_settings, "nearby_panic_schedule_json", "[]"))):
        return False

    receive_from = str(getattr(recipient_settings, "nearby_panic_receive_from", "everyone") or "everyone").lower()
    if receive_from == "verified_only" and not bool(getattr(sender, "email_verified", False)):
        return False
    if receive_from == "same_area":
        sender_area = ""
        recipient_area = str(getattr(recipient_settings, "nearby_panic_same_area_label", "") or "").strip().lower()
        sender_settings = getattr(sender, "_panic_settings_cache", None)
        if sender_settings is not None:
            sender_area = str(getattr(sender_settings, "nearby_panic_same_area_label", "") or "").strip().lower()
        if not recipient_area or recipient_area != sender_area:
            return False

    return True


def _log_audit(db: Session, *, actor_user_id: str | None, action: str, resource_type: str, resource_id: str, meta: dict[str, Any]) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            meta_json=_json_dumps(meta),
        )
    )


def _emit(event: str, payload: dict[str, Any], *, rooms: Iterable[str]) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        for room in rooms:
            try:
                from_thread.run(sio.emit, event, {"data": payload}, room=room, namespace=settings.DASHBOARD_NAMESPACE)
            except Exception:
                return
        return

    for room in rooms:
        sio.start_background_task(
            sio.emit,
            event,
            {"data": payload},
            room=room,
            namespace=settings.DASHBOARD_NAMESPACE,
        )


def _resolve_context(db: Session, user: User) -> dict[str, Any]:
    if user.role == UserRole.homeowner:
        home = (
            db.query(Home)
            .filter(Home.homeowner_id == user.id)
            .order_by(Home.created_at.desc())
            .first()
        )
        if not home or not home.estate_id:
            raise AppException("Your home is not linked to an estate yet.", status_code=400)
        estate = db.query(Estate).filter(Estate.id == home.estate_id).first()
        if not estate:
            raise AppException("Estate not found.", status_code=404)
        return {"estate": estate, "home": home, "unitLabel": home.name}

    if not user.estate_id:
        raise AppException("This account is not linked to an estate.", status_code=400)
    estate = db.query(Estate).filter(Estate.id == user.estate_id).first()
    if not estate:
        raise AppException("Estate not found.", status_code=404)
    return {"estate": estate, "home": None, "unitLabel": None}


def _list_security_users(db: Session, estate_id: str) -> list[User]:
    rows = (
        db.query(User)
        .filter(
            User.estate_id == estate_id,
            User.role.in_([UserRole.security, UserRole.estate, UserRole.admin]),
            User.is_active.is_(True),
        )
        .order_by(User.full_name.asc())
        .all()
    )
    estate = db.query(Estate).filter(Estate.id == estate_id).first()
    if estate and estate.owner_id:
        owner = db.query(User).filter(User.id == estate.owner_id, User.is_active.is_(True)).first()
        if owner and owner.id not in {row.id for row in rows}:
            rows.append(owner)
    return rows


def _alert_rooms(alert: EmergencyAlert, recipient_ids: list[str]) -> list[str]:
    rooms = [
        f"estate:{alert.estate_id}:alerts",
        f"estate:{alert.estate_id}:safety",
        f"user:{alert.user_id}",
    ]
    for recipient_id in recipient_ids:
        rooms.append(f"user:{recipient_id}")
    return rooms


def serialize_alert_event(event: EmergencyAlertEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "eventType": event.event_type,
        "channel": event.channel,
        "deliveryStatus": event.delivery_status.value if hasattr(event.delivery_status, "value") else str(event.delivery_status),
        "targetType": event.target_type,
        "targetUserId": event.target_user_id,
        "targetLabel": event.target_label,
        "metadata": _json_loads(event.metadata_json),
        "createdAt": event.created_at.isoformat() if event.created_at else None,
    }


def serialize_alert(db: Session, alert: EmergencyAlert) -> dict[str, Any]:
    events = (
        db.query(EmergencyAlertEvent)
        .filter(EmergencyAlertEvent.alert_id == alert.id)
        .order_by(EmergencyAlertEvent.created_at.asc())
        .all()
    )
    return {
        "id": alert.id,
        "estateId": alert.estate_id,
        "homeId": alert.home_id,
        "userId": alert.user_id,
        "alertType": alert.alert_type.value if hasattr(alert.alert_type, "value") else str(alert.alert_type),
        "priority": alert.priority.value if hasattr(alert.priority, "value") else str(alert.priority),
        "status": alert.status.value if hasattr(alert.status, "value") else str(alert.status),
        "unitLabel": alert.unit_label,
        "triggerMode": alert.trigger_mode,
        "silentTrigger": bool(alert.silent_trigger),
        "offlineQueued": bool(alert.offline_queued),
        "cancelWindowSeconds": int(alert.cancel_window_seconds or 0),
        "cancelExpiresAt": alert.cancel_expires_at.isoformat() if alert.cancel_expires_at else None,
        "acknowledgedAt": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        "escalatedAt": alert.escalated_at.isoformat() if alert.escalated_at else None,
        "resolvedAt": alert.resolved_at.isoformat() if alert.resolved_at else None,
        "location": {
            "lat": _to_float(alert.last_known_lat),
            "lng": _to_float(alert.last_known_lng),
            "address": alert.last_known_address,
            "source": alert.last_known_source,
        },
        "notes": alert.notes or "",
        "triggeredAt": alert.triggered_at.isoformat() if alert.triggered_at else None,
        "updatedAt": alert.updated_at.isoformat() if alert.updated_at else None,
        "events": [serialize_alert_event(event) for event in events],
    }


def _resolve_panic_context(db: Session, user: User) -> dict[str, Any]:
    if user.role == UserRole.homeowner:
        home = (
            db.query(Home)
            .filter(Home.homeowner_id == user.id)
            .order_by(Home.created_at.desc())
            .first()
        )
        estate = db.query(Estate).filter(Estate.id == home.estate_id).first() if home and home.estate_id else None
        return {
            "home": home,
            "estate": estate,
            "mode": PanicMode.estate if estate else PanicMode.personal,
            "unitLabel": home.name if home else None,
        }

    estate = db.query(Estate).filter(Estate.id == user.estate_id).first() if user.estate_id else None
    if not estate and user.role != UserRole.admin:
        raise AppException("This account is not linked to an estate.", status_code=400)
    return {
        "home": None,
        "estate": estate,
        "mode": PanicMode.estate if estate else PanicMode.personal,
        "unitLabel": None,
    }


def _known_contact_matches_user(contact_line: str, candidate: User) -> bool:
    contact_line = str(contact_line or "").strip()
    if not contact_line:
        return False

    email_match = _normalize_email(candidate.email) and _normalize_email(candidate.email) in _normalize_email(contact_line)
    phone_match = _normalize_phone(candidate.phone) and _normalize_phone(candidate.phone) in _normalize_phone(contact_line)
    name_match = _normalize_name(candidate.full_name) and _normalize_name(candidate.full_name) in _normalize_name(contact_line)
    return bool(email_match or phone_match or name_match)


def _list_personal_contact_users(db: Session, *, homeowner: User, settings_row: HomeownerSetting | None) -> list[User]:
    known_contacts = []
    if settings_row:
        try:
            known_contacts = json.loads(settings_row.known_contacts_json or "[]")
        except Exception:
            known_contacts = []
    known_contacts = [str(item or "").strip() for item in known_contacts if str(item or "").strip()]
    if not known_contacts:
        return []

    candidates = (
        db.query(User)
        .filter(User.id != homeowner.id, User.is_active.is_(True))
        .order_by(User.full_name.asc())
        .all()
    )
    matches: list[User] = []
    seen_ids: set[str] = set()
    for contact_line in known_contacts:
        for candidate in candidates:
            if candidate.id in seen_ids:
                continue
            if _known_contact_matches_user(contact_line, candidate):
                matches.append(candidate)
                seen_ids.add(candidate.id)
    return matches


def _list_estate_homeowner_users(db: Session, *, estate_id: str, exclude_user_id: str) -> list[User]:
    rows = (
        db.query(User)
        .join(Home, Home.homeowner_id == User.id)
        .filter(
            Home.estate_id == estate_id,
            User.role == UserRole.homeowner,
            User.is_active.is_(True),
            User.id != exclude_user_id,
        )
        .order_by(User.full_name.asc())
        .all()
    )
    return _dedupe_users(rows)


def _community_panic_recipients(
    db: Session,
    *,
    homeowner: User,
    settings_row: HomeownerSetting | None,
    location: dict[str, Any] | None,
    now: datetime,
) -> list[User]:
    lat = (location or {}).get("lat")
    lng = (location or {}).get("lng")
    if lat is None or lng is None:
        return []

    candidates = (
        db.query(User, HomeownerSetting)
        .join(HomeownerSetting, HomeownerSetting.user_id == User.id)
        .filter(
            User.id != homeowner.id,
            User.role == UserRole.homeowner,
            User.is_active.is_(True),
        )
        .all()
    )

    if settings_row is not None:
        setattr(homeowner, "_panic_settings_cache", settings_row)

    radius_limit = max(200, min(int(getattr(settings_row, "nearby_panic_alert_radius_m", DEFAULT_NEARBY_PANIC_RADIUS_M) or DEFAULT_NEARBY_PANIC_RADIUS_M), 1000))
    rows: list[User] = []
    for candidate, candidate_settings in candidates:
        recipient_lat = getattr(candidate_settings, "safety_home_lat", None)
        recipient_lng = getattr(candidate_settings, "safety_home_lng", None)
        if recipient_lat is None or recipient_lng is None:
            continue
        try:
            distance = _distance_meters(float(lat), float(lng), float(recipient_lat), float(recipient_lng))
        except Exception:
            continue
        recipient_radius = max(200, min(int(getattr(candidate_settings, "nearby_panic_alert_radius_m", DEFAULT_NEARBY_PANIC_RADIUS_M) or DEFAULT_NEARBY_PANIC_RADIUS_M), 1000))
        effective_radius = min(radius_limit, recipient_radius)
        if distance > effective_radius:
            continue
        if not _recipient_allows_panic(homeowner, candidate, candidate_settings, now=now):
            continue
        rows.append(candidate)
    return _dedupe_users(rows)


def _panic_recipient_users(
    db: Session,
    *,
    homeowner: User,
    estate_id: str | None,
    settings_row: HomeownerSetting | None,
    location: dict[str, Any] | None,
    now: datetime,
) -> list[User]:
    recipients = _list_personal_contact_users(db, homeowner=homeowner, settings_row=settings_row)

    if estate_id:
        recipients.extend(_list_security_users(db, estate_id))

    recipients.extend(
        _community_panic_recipients(
            db,
            homeowner=homeowner,
            settings_row=settings_row,
            location=location,
            now=now,
        )
    )

    return _dedupe_users(recipients)


def _panic_rooms(panic: PanicEvent, recipient_ids: list[str]) -> list[str]:
    rooms = {
        f"user:{panic.user_id}",
        f"user_{panic.user_id}",
        f"contacts:{panic.user_id}",
        f"contacts_{panic.user_id}",
    }
    if panic.estate_id:
        rooms.update(
            {
                f"estate:{panic.estate_id}:panic",
                f"estate_{panic.estate_id}",
            }
        )
    for recipient_id in recipient_ids:
        rooms.update({f"user:{recipient_id}", f"user_{recipient_id}"})
    return list(rooms)


def serialize_panic_event(db: Session, panic: PanicEvent) -> dict[str, Any]:
    trigger_user = db.query(User).filter(User.id == panic.user_id).first()
    responders = _parse_json_list(getattr(panic, "responder_details_json", "[]"))
    false_reports = _parse_recipient_user_ids(getattr(panic, "false_report_user_ids_json", "[]"))
    ignored_user_ids = _parse_recipient_user_ids(getattr(panic, "ignored_user_ids_json", "[]"))
    return {
        "id": panic.id,
        "panicId": panic.id,
        "userId": panic.user_id,
        "userName": trigger_user.full_name if trigger_user else "Resident",
        "userPhone": trigger_user.phone if trigger_user else None,
        "estateId": panic.estate_id,
        "homeId": panic.home_id,
        "type": "panic",
        "mode": panic.mode.value if hasattr(panic.mode, "value") else str(panic.mode),
        "status": panic.status.value if hasattr(panic.status, "value") else str(panic.status),
        "acknowledged": bool(panic.acknowledged),
        "unitLabel": panic.unit_label,
        "location": {
            "doorName": panic.unit_label,
            "address": panic.last_known_address,
            "blurredAddress": _blur_location_label(panic.last_known_address, panic.unit_label),
            "lat": _to_float(panic.last_known_lat),
            "lng": _to_float(panic.last_known_lng),
            "source": panic.last_known_source,
        },
        "triggerTrustScore": int(getattr(panic, "trigger_trust_score", 0) or 0),
        "responderUserIds": _parse_recipient_user_ids(getattr(panic, "responder_user_ids_json", "[]")),
        "responders": responders,
        "responderCount": len(responders),
        "ignoredUserIds": ignored_user_ids,
        "falseReportCount": len(false_reports),
        "responseStartedAt": panic.response_started_at.isoformat() if getattr(panic, "response_started_at", None) else None,
        "lastResponderAt": panic.last_responder_at.isoformat() if getattr(panic, "last_responder_at", None) else None,
        "incidentNotes": getattr(panic, "incident_notes", None),
        "retryCount": int(panic.retry_count or 0),
        "recipientUserIds": _parse_recipient_user_ids(panic.recipient_user_ids_json),
        "acknowledgedAt": panic.acknowledged_at.isoformat() if panic.acknowledged_at else None,
        "resolvedAt": panic.resolved_at.isoformat() if panic.resolved_at else None,
        "lastDispatchedAt": panic.last_dispatched_at.isoformat() if panic.last_dispatched_at else None,
        "createdAt": panic.created_at.isoformat() if panic.created_at else None,
        "updatedAt": panic.updated_at.isoformat() if panic.updated_at else None,
        "timestamp": panic.created_at.isoformat() if panic.created_at else None,
    }


def _emit_panic_state(db: Session, panic: PanicEvent, *, event_name: str = "panic_alert") -> dict[str, Any]:
    payload = serialize_panic_event(db, panic)
    _emit(event_name, payload, rooms=_panic_rooms(panic, payload["recipientUserIds"]))
    return payload


async def _panic_retry_loop(panic_id: str) -> None:
    for attempt in range(1, PANIC_MAX_RETRIES + 1):
        await asyncio.sleep(PANIC_RETRY_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            panic = db.query(PanicEvent).filter(PanicEvent.id == panic_id).first()
            if not panic:
                return
            if panic.status == PanicEventStatus.resolved or panic.acknowledged:
                return
            panic.retry_count = int(panic.retry_count or 0) + 1
            panic.last_dispatched_at = utc_now()
            db.commit()
            db.refresh(panic)
            _emit_panic_state(db, panic, event_name="panic_alert")
        except Exception:
            db.rollback()
            return
        finally:
            db.close()


def _schedule_panic_retries(panic_id: str) -> None:
    try:
        sio.start_background_task(_panic_retry_loop, panic_id)
    except Exception:
        return


def _can_access_panic(db: Session, *, panic: PanicEvent, actor: User) -> bool:
    if actor.role == UserRole.admin:
        return True
    if actor.id == panic.user_id:
        return True
    if panic.estate_id and actor.estate_id == panic.estate_id and actor.role in {UserRole.security, UserRole.estate}:
        return True
    return actor.id in set(_parse_recipient_user_ids(panic.recipient_user_ids_json))


def _load_panic_for_actor(db: Session, *, panic_id: str, actor: User) -> PanicEvent:
    panic = db.query(PanicEvent).filter(PanicEvent.id == panic_id).first()
    if not panic:
        raise AppException("Panic event not found.", status_code=404)
    if not _can_access_panic(db, panic=panic, actor=actor):
        raise AppException("You do not have access to this panic event.", status_code=403)
    return panic


def trigger_panic_event(
    db: Session,
    *,
    actor: User,
    user_id: str | None = None,
    trigger_mode: str = "hold",
    location: dict[str, Any] | None = None,
    offline_queued: bool = False,
) -> dict[str, Any]:
    target_user = actor
    if user_id and actor.role == UserRole.admin:
        loaded = db.query(User).filter(User.id == user_id).first()
        if not loaded:
            raise AppException("User not found.", status_code=404)
        target_user = loaded
    elif user_id and str(user_id) != str(actor.id):
        raise AppException("You can only trigger panic for your own account.", status_code=403)

    if target_user.role != UserRole.homeowner:
        raise AppException("Panic alerts can only be triggered for homeowner accounts.", status_code=400)

    context = _resolve_panic_context(db, target_user)
    settings_row = db.query(HomeownerSetting).filter(HomeownerSetting.user_id == target_user.id).first()
    one_hour_ago = utc_now() - timedelta(hours=1)
    recent_triggers = (
        db.query(func.count(PanicEvent.id))
        .filter(PanicEvent.user_id == target_user.id, PanicEvent.created_at >= one_hour_ago)
        .scalar()
        or 0
    )
    if int(recent_triggers) >= PANIC_MAX_TRIGGERS_PER_HOUR:
        raise AppException("Panic trigger limit reached. Please contact support if this is an active emergency.", status_code=429)

    now = utc_now()
    recipients = _panic_recipient_users(
        db,
        homeowner=target_user,
        estate_id=context["estate"].id if context["estate"] else None,
        settings_row=settings_row,
        location=location,
        now=now,
    )
    personal_contact_ids = {row.id for row in _list_personal_contact_users(db, homeowner=target_user, settings_row=settings_row)}
    panic = PanicEvent(
        user_id=target_user.id,
        estate_id=context["estate"].id if context["estate"] else None,
        home_id=context["home"].id if context["home"] else None,
        type="panic",
        mode=context["mode"],
        status=PanicEventStatus.active,
        acknowledged=False,
        unit_label=context["unitLabel"] or (location or {}).get("doorName") or (location or {}).get("address"),
        last_known_lat=(location or {}).get("lat"),
        last_known_lng=(location or {}).get("lng"),
        last_known_address=(location or {}).get("address"),
        last_known_source=(location or {}).get("source") or trigger_mode,
        recipient_user_ids_json=json.dumps([recipient.id for recipient in recipients], ensure_ascii=True),
        responder_user_ids_json="[]",
        responder_details_json="[]",
        ignored_user_ids_json="[]",
        false_report_user_ids_json="[]",
        trigger_trust_score=_panic_trigger_trust_score(target_user),
        incident_notes=None,
        response_started_at=None,
        last_responder_at=None,
        retry_count=0,
        last_dispatched_at=now,
        created_at=now,
    )
    db.add(panic)
    db.flush()
    _log_audit(
        db,
        actor_user_id=actor.id,
        action="panic.triggered",
        resource_type="panic_event",
        resource_id=panic.id,
        meta={
            "mode": panic.mode.value if hasattr(panic.mode, "value") else str(panic.mode),
            "offlineQueued": bool(offline_queued),
            "recipientCount": len(recipients),
        },
    )
    db.commit()
    db.refresh(panic)

    for recipient in recipients:
        recipient_settings = db.query(HomeownerSetting).filter(HomeownerSetting.user_id == recipient.id).first()
        approx_distance_m = None
        try:
            if (
                (location or {}).get("lat") is not None
                and (location or {}).get("lng") is not None
                and recipient_settings
                and recipient_settings.safety_home_lat is not None
                and recipient_settings.safety_home_lng is not None
            ):
                approx_distance_m = _bucket_distance(
                    _distance_meters(
                        float((location or {}).get("lat")),
                        float((location or {}).get("lng")),
                        float(recipient_settings.safety_home_lat),
                        float(recipient_settings.safety_home_lng),
                    )
                )
        except Exception:
            approx_distance_m = None

        distance_text = f"{int(approx_distance_m)}m away" if approx_distance_m and approx_distance_m < 1000 else (
            f"{round(approx_distance_m / 1000, 1)}km away" if approx_distance_m else "near you"
        )
        is_public_recipient = recipient.role == UserRole.homeowner and recipient.id not in personal_contact_ids
        sender_label = (
            "Nearby QRing user"
            if is_public_recipient and str(getattr(settings_row, "panic_identity_visibility", "masked") or "masked").lower() == "masked"
            else target_user.full_name
        )
        notification_message = f"{sender_label} triggered a panic alert {distance_text}. {_blur_location_label(panic.last_known_address, panic.unit_label)}."
        create_notification(
            db=db,
            user_id=recipient.id,
            kind="safety.panic",
            payload={
                "panicId": panic.id,
                "userId": target_user.id,
                "userName": sender_label,
                "userPhone": target_user.phone,
                "mode": panic.mode.value if hasattr(panic.mode, "value") else str(panic.mode),
                "unitLabel": panic.unit_label,
                "message": notification_message,
                "approxDistanceMeters": approx_distance_m,
                "blurredLocation": _blur_location_label(panic.last_known_address, panic.unit_label),
                "identityMasked": sender_label != target_user.full_name,
                "actionSet": "panic_response" if recipient.role == UserRole.homeowner else "",
                "route": (
                    "/dashboard/estate/emergency"
                    if recipient.role == UserRole.estate
                    else "/dashboard/security/emergency"
                    if recipient.role == UserRole.security
                    else "/dashboard/homeowner/safety"
                ),
                "sound": "panic_alert",
                "priority": "critical",
            },
        )
        if recipient.email:
            send_email_smtp(
                to_email=recipient.email,
                subject="Qring Panic Alert",
                body=notification_message,
            )

    payload = _emit_panic_state(db, panic, event_name="panic_alert")
    _schedule_panic_retries(panic.id)
    return payload


def acknowledge_panic_event(db: Session, *, panic_id: str, actor: User) -> dict[str, Any]:
    panic = _load_panic_for_actor(db, panic_id=panic_id, actor=actor)
    if panic.status == PanicEventStatus.resolved:
        raise AppException("Resolved panic events cannot be acknowledged.", status_code=400)

    panic.acknowledged = True
    panic.acknowledged_by_user_id = actor.id
    panic.acknowledged_at = utc_now()
    _log_audit(
        db,
        actor_user_id=actor.id,
        action="panic.acknowledged",
        resource_type="panic_event",
        resource_id=panic.id,
        meta={},
    )
    db.commit()
    db.refresh(panic)
    payload = _emit_panic_state(db, panic, event_name="panic_alert_update")
    return payload


def respond_to_panic_event(db: Session, *, panic_id: str, actor: User) -> dict[str, Any]:
    panic = _load_panic_for_actor(db, panic_id=panic_id, actor=actor)
    if actor.role not in {UserRole.homeowner, UserRole.security, UserRole.estate, UserRole.admin}:
        raise AppException("You cannot respond to this panic event.", status_code=403)
    if panic.status == PanicEventStatus.resolved:
        raise AppException("Resolved panic events cannot be responded to.", status_code=400)

    responder_ids = _parse_recipient_user_ids(getattr(panic, "responder_user_ids_json", "[]"))
    if actor.id not in responder_ids:
        responder_ids.append(actor.id)
    details = _parse_json_list(getattr(panic, "responder_details_json", "[]"))
    if not any(str(item.get("userId") or "").strip() == actor.id for item in details):
        details.append(
            {
                "userId": actor.id,
                "name": actor.full_name,
                "role": actor.role.value if hasattr(actor.role, "value") else str(actor.role),
                "respondedAt": utc_now().isoformat(),
                "phone": actor.phone,
            }
        )
    now = utc_now()
    panic.responder_user_ids_json = json.dumps(responder_ids, ensure_ascii=True)
    panic.responder_details_json = json.dumps(details, ensure_ascii=True)
    panic.response_started_at = panic.response_started_at or now
    panic.last_responder_at = now
    panic.acknowledged = True
    panic.acknowledged_by_user_id = panic.acknowledged_by_user_id or actor.id
    panic.acknowledged_at = panic.acknowledged_at or now
    _log_audit(
        db,
        actor_user_id=actor.id,
        action="panic.responding",
        resource_type="panic_event",
        resource_id=panic.id,
        meta={},
    )
    db.commit()
    db.refresh(panic)
    create_notification(
        db=db,
        user_id=panic.user_id,
        kind="safety.panic.response",
        payload={
            "panicId": panic.id,
            "responderUserId": actor.id,
            "responderName": actor.full_name,
            "message": f"{actor.full_name} is responding to your panic alert.",
            "route": "/dashboard/homeowner/safety",
            "priority": "critical",
        },
    )
    return _emit_panic_state(db, panic, event_name="panic_alert_update")


def ignore_panic_event(db: Session, *, panic_id: str, actor: User) -> dict[str, Any]:
    panic = _load_panic_for_actor(db, panic_id=panic_id, actor=actor)
    ignored_user_ids = _parse_recipient_user_ids(getattr(panic, "ignored_user_ids_json", "[]"))
    if actor.id not in ignored_user_ids:
        ignored_user_ids.append(actor.id)
    panic.ignored_user_ids_json = json.dumps(ignored_user_ids, ensure_ascii=True)
    _log_audit(
        db,
        actor_user_id=actor.id,
        action="panic.ignored",
        resource_type="panic_event",
        resource_id=panic.id,
        meta={},
    )
    db.commit()
    db.refresh(panic)
    return _emit_panic_state(db, panic, event_name="panic_alert_update")


def report_false_panic_event(db: Session, *, panic_id: str, actor: User) -> dict[str, Any]:
    panic = _load_panic_for_actor(db, panic_id=panic_id, actor=actor)
    reported_by = _parse_recipient_user_ids(getattr(panic, "false_report_user_ids_json", "[]"))
    if actor.id not in reported_by:
        reported_by.append(actor.id)
    panic.false_report_user_ids_json = json.dumps(reported_by, ensure_ascii=True)
    _log_audit(
        db,
        actor_user_id=actor.id,
        action="panic.reported_false",
        resource_type="panic_event",
        resource_id=panic.id,
        meta={"reportedCount": len(reported_by)},
    )
    db.commit()
    db.refresh(panic)
    if len(reported_by) == 1:
        create_notification(
            db=db,
            user_id=panic.user_id,
            kind="safety.panic.reported",
            payload={
                "panicId": panic.id,
                "message": "A nearby responder reported this panic alert for review.",
                "route": "/dashboard/homeowner/safety",
                "priority": "normal",
            },
        )
    return _emit_panic_state(db, panic, event_name="panic_alert_update")


def update_panic_event_notes(db: Session, *, panic_id: str, actor: User, notes: str) -> dict[str, Any]:
    panic = _load_panic_for_actor(db, panic_id=panic_id, actor=actor)
    panic.incident_notes = (notes or "").strip() or None
    _log_audit(
        db,
        actor_user_id=actor.id,
        action="panic.notes_updated",
        resource_type="panic_event",
        resource_id=panic.id,
        meta={},
    )
    db.commit()
    db.refresh(panic)
    return _emit_panic_state(db, panic, event_name="panic_alert_update")


def resolve_panic_event(db: Session, *, panic_id: str, actor: User) -> dict[str, Any]:
    panic = _load_panic_for_actor(db, panic_id=panic_id, actor=actor)
    panic.status = PanicEventStatus.resolved
    panic.acknowledged = True
    panic.acknowledged_by_user_id = panic.acknowledged_by_user_id or actor.id
    panic.acknowledged_at = panic.acknowledged_at or utc_now()
    panic.resolved_by_user_id = actor.id
    panic.resolved_at = utc_now()
    _log_audit(
        db,
        actor_user_id=actor.id,
        action="panic.resolved",
        resource_type="panic_event",
        resource_id=panic.id,
        meta={},
    )
    db.commit()
    db.refresh(panic)
    return _emit_panic_state(db, panic, event_name="panic_alert_update")


def list_active_panic_events(db: Session, *, actor: User) -> list[dict[str, Any]]:
    rows = (
        db.query(PanicEvent)
        .filter(PanicEvent.status == PanicEventStatus.active)
        .order_by(PanicEvent.created_at.desc())
        .limit(100)
        .all()
    )
    visible = [
        panic
        for panic in rows
        if _can_access_panic(db, panic=panic, actor=actor)
        and (
            actor.role in {UserRole.security, UserRole.estate, UserRole.admin}
            or actor.id == panic.user_id
            or actor.id not in set(_parse_recipient_user_ids(getattr(panic, "ignored_user_ids_json", "[]")))
        )
    ]
    return [serialize_panic_event(db, panic) for panic in visible]


def serialize_watchlist_entry(entry: WatchlistEntry, *, recent_reports: list[VisitorReport] | None = None) -> dict[str, Any]:
    return {
        "id": entry.id,
        "estateId": entry.estate_id,
        "displayName": entry.display_name,
        "displayPhone": entry.display_phone,
        "riskLevel": entry.risk_level.value if hasattr(entry.risk_level, "value") else str(entry.risk_level),
        "reportCount": int(entry.report_count or 0),
        "active": bool(entry.active),
        "blocked": bool(entry.blocked),
        "autoFlagged": bool(entry.auto_flagged),
        "lastReportedAt": entry.last_reported_at.isoformat() if entry.last_reported_at else None,
        "history": [
            {
                "id": report.id,
                "reason": report.reason,
                "severity": report.severity.value if hasattr(report.severity, "value") else str(report.severity),
                "status": report.status.value if hasattr(report.status, "value") else str(report.status),
                "createdAt": report.created_at.isoformat() if report.created_at else None,
            }
            for report in (recent_reports or [])
        ],
    }


def serialize_visitor_report(report: VisitorReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "estateId": report.estate_id,
        "visitorSessionId": report.visitor_session_id,
        "reporterUserId": report.reporter_user_id,
        "hostUserId": report.host_user_id,
        "reportedName": report.reported_name,
        "reportedPhone": report.reported_phone,
        "reason": report.reason,
        "notes": report.notes or "",
        "severity": report.severity.value if hasattr(report.severity, "value") else str(report.severity),
        "status": report.status.value if hasattr(report.status, "value") else str(report.status),
        "occurrenceCount": int(report.occurrence_count or 1),
        "createdAt": report.created_at.isoformat() if report.created_at else None,
        "updatedAt": report.updated_at.isoformat() if report.updated_at else None,
    }


def trigger_emergency_alert(
    db: Session,
    *,
    user: User,
    alert_type: str,
    trigger_mode: str = "hold",
    silent_trigger: bool = False,
    cancel_window_seconds: int = 8,
    location: dict[str, Any] | None = None,
    offline_queued: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    context = _resolve_context(db, user)
    estate: Estate = context["estate"]
    home: Home | None = context["home"]
    settings_row = db.query(HomeownerSetting).filter(HomeownerSetting.user_id == user.id).first() if user.role == UserRole.homeowner else None

    try:
        normalized_type = EmergencyAlertType(str(alert_type).strip().lower())
    except Exception as exc:
        raise AppException("Unsupported alert type.", status_code=400) from exc

    now = utc_now()
    cancel_window_seconds = max(5, min(int(cancel_window_seconds or 8), 10))
    alert = EmergencyAlert(
        estate_id=estate.id,
        home_id=home.id if home else None,
        user_id=user.id,
        alert_type=normalized_type,
        priority=PRIORITY_BY_TYPE[normalized_type],
        status=EmergencyAlertStatus.dispatched,
        unit_label=context["unitLabel"],
        trigger_mode=(trigger_mode or "hold").strip().lower(),
        silent_trigger=bool(silent_trigger),
        offline_queued=bool(offline_queued),
        cancel_window_seconds=cancel_window_seconds,
        cancel_expires_at=now + timedelta(seconds=cancel_window_seconds),
        last_known_lat=location.get("lat") if isinstance(location, dict) else None,
        last_known_lng=location.get("lng") if isinstance(location, dict) else None,
        last_known_address=location.get("address") if isinstance(location, dict) else None,
        last_known_source=location.get("source") if isinstance(location, dict) else None,
        notes=(notes or "").strip() or None,
        triggered_at=now,
    )
    db.add(alert)
    db.flush()

    recipients = _list_security_users(db, estate.id)
    event_rows = [
        EmergencyAlertEvent(
            alert_id=alert.id,
            actor_user_id=user.id,
            event_type="triggered",
            channel="internet",
            delivery_status=AlertDeliveryStatus.sent,
            target_type="system",
            target_label="QRing realtime gateway",
            metadata_json=_json_dumps({"offlineQueued": bool(offline_queued), "silentTrigger": bool(silent_trigger)}),
        )
    ]
    for recipient in recipients:
        event_rows.append(
            EmergencyAlertEvent(
                alert_id=alert.id,
                actor_user_id=user.id,
                event_type="security_notified",
                channel="internet",
                delivery_status=AlertDeliveryStatus.received,
                target_type="user",
                target_user_id=recipient.id,
                target_label=recipient.full_name,
                metadata_json=_json_dumps({"role": recipient.role.value}),
            )
        )
    if settings_row and settings_row.sms_fallback_enabled:
        event_rows.append(
            EmergencyAlertEvent(
                alert_id=alert.id,
                actor_user_id=user.id,
                event_type="sms_fallback_queued",
                channel="sms",
                delivery_status=AlertDeliveryStatus.queued,
                target_type="fallback",
                target_label="Configured fallback contacts",
                metadata_json=_json_dumps({"source": "homeowner_settings"}),
            )
        )
    db.add_all(event_rows)
    _log_audit(
        db,
        actor_user_id=user.id,
        action="emergency_alert.triggered",
        resource_type="emergency_alert",
        resource_id=alert.id,
        meta={
            "estateId": estate.id,
            "homeId": home.id if home else None,
            "alertType": normalized_type.value,
            "silentTrigger": bool(silent_trigger),
            "offlineQueued": bool(offline_queued),
        },
    )
    db.commit()
    db.refresh(alert)

    message = f"{SECURITY_MESSAGE_BY_TYPE[normalized_type]} Unit: {context['unitLabel'] or 'Unknown'}."
    for recipient in recipients:
        create_notification(
            db=db,
            user_id=recipient.id,
            kind="safety.emergency",
            payload={
                "alertId": alert.id,
                "estateId": estate.id,
                "alertType": normalized_type.value,
                "priority": alert.priority.value,
                "unitLabel": context["unitLabel"],
                "silentTrigger": bool(silent_trigger),
                "message": message,
            },
        )

    serialized = serialize_alert(db, alert)
    _emit("safety.alert.created", serialized, rooms=_alert_rooms(alert, [recipient.id for recipient in recipients]))
    return serialized


def _load_alert_for_actor(db: Session, *, alert_id: str, actor: User) -> EmergencyAlert:
    alert = db.query(EmergencyAlert).filter(EmergencyAlert.id == alert_id).first()
    if not alert:
        raise AppException("Emergency alert not found.", status_code=404)

    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    if alert.estate_id != estate.id and actor.role != UserRole.admin:
        raise AppException("You do not have access to this emergency alert.", status_code=403)
    return alert


def _append_alert_event(
    db: Session,
    *,
    alert: EmergencyAlert,
    actor_user_id: str | None,
    event_type: str,
    channel: str,
    delivery_status: AlertDeliveryStatus,
    target_type: str | None = None,
    target_user_id: str | None = None,
    target_label: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        EmergencyAlertEvent(
            alert_id=alert.id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            channel=channel,
            delivery_status=delivery_status,
            target_type=target_type,
            target_user_id=target_user_id,
            target_label=target_label,
            metadata_json=_json_dumps(metadata),
        )
    )


def cancel_emergency_alert(db: Session, *, alert_id: str, user: User, reason: str | None = None) -> dict[str, Any]:
    alert = _load_alert_for_actor(db, alert_id=alert_id, actor=user)
    now = utc_now()
    if alert.user_id != user.id:
        raise AppException("Only the resident who triggered this alert can cancel it.", status_code=403)
    if alert.status in {EmergencyAlertStatus.acknowledged, EmergencyAlertStatus.escalated, EmergencyAlertStatus.resolved}:
        raise AppException("This alert is already being handled and can no longer be cancelled.", status_code=400)
    if alert.cancel_expires_at and now > alert.cancel_expires_at:
        raise AppException("The cancel window has expired.", status_code=400)

    alert.status = EmergencyAlertStatus.cancelled
    alert.notes = reason or alert.notes
    _append_alert_event(
        db,
        alert=alert,
        actor_user_id=user.id,
        event_type="cancelled",
        channel="internet",
        delivery_status=AlertDeliveryStatus.acknowledged,
        target_type="resident",
        target_user_id=user.id,
        target_label=user.full_name,
        metadata={"reason": reason or ""},
    )
    _log_audit(
        db,
        actor_user_id=user.id,
        action="emergency_alert.cancelled",
        resource_type="emergency_alert",
        resource_id=alert.id,
        meta={"reason": reason or ""},
    )
    db.commit()
    db.refresh(alert)
    serialized = serialize_alert(db, alert)
    _emit("safety.alert.updated", serialized, rooms=_alert_rooms(alert, []))
    return serialized


def update_emergency_alert_status(
    db: Session,
    *,
    alert_id: str,
    actor: User,
    action: str,
    notes: str | None = None,
) -> dict[str, Any]:
    alert = _load_alert_for_actor(db, alert_id=alert_id, actor=actor)
    normalized_action = str(action or "").strip().lower()
    now = utc_now()

    if normalized_action == "acknowledge":
        if alert.status == EmergencyAlertStatus.cancelled:
            raise AppException("Cancelled alerts cannot be acknowledged.", status_code=400)
        alert.status = EmergencyAlertStatus.acknowledged
        alert.acknowledged_by_user_id = actor.id
        alert.acknowledged_at = now
        event_type = "acknowledged"
    elif normalized_action == "escalate":
        if alert.status == EmergencyAlertStatus.cancelled:
            raise AppException("Cancelled alerts cannot be escalated.", status_code=400)
        alert.status = EmergencyAlertStatus.escalated
        alert.escalated_at = now
        event_type = "escalated"
    elif normalized_action == "resolve":
        alert.status = EmergencyAlertStatus.resolved
        alert.resolved_by_user_id = actor.id
        alert.resolved_at = now
        event_type = "resolved"
    else:
        raise AppException("Unsupported alert action.", status_code=400)

    if notes:
        alert.notes = notes.strip()
    _append_alert_event(
        db,
        alert=alert,
        actor_user_id=actor.id,
        event_type=event_type,
        channel="internet",
        delivery_status=AlertDeliveryStatus.acknowledged,
        target_type="operator",
        target_user_id=actor.id,
        target_label=actor.full_name,
        metadata={"notes": notes or ""},
    )
    _log_audit(
        db,
        actor_user_id=actor.id,
        action=f"emergency_alert.{event_type}",
        resource_type="emergency_alert",
        resource_id=alert.id,
        meta={"notes": notes or ""},
    )
    db.commit()
    db.refresh(alert)
    serialized = serialize_alert(db, alert)
    _emit("safety.alert.updated", serialized, rooms=_alert_rooms(alert, []))
    return serialized


def list_emergency_alerts(db: Session, *, actor: User, limit: int = 40) -> list[dict[str, Any]]:
    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    query = db.query(EmergencyAlert).filter(EmergencyAlert.estate_id == estate.id)
    if actor.role == UserRole.homeowner:
        query = query.filter(EmergencyAlert.user_id == actor.id)
    rows = query.order_by(EmergencyAlert.triggered_at.desc()).limit(max(1, min(limit, 100))).all()
    return [serialize_alert(db, row) for row in rows]


def _severity_rank(severity: VisitorReportSeverity | str) -> int:
    value = severity.value if hasattr(severity, "value") else str(severity)
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(value, 2)


def _risk_for_report_count(count: int, severity: VisitorReportSeverity | str) -> WatchlistRiskLevel:
    score = max(count, _severity_rank(severity))
    if score >= 4:
        return WatchlistRiskLevel.critical
    if score >= 3:
        return WatchlistRiskLevel.high
    if score >= 2:
        return WatchlistRiskLevel.medium
    return WatchlistRiskLevel.low


def report_visitor(
    db: Session,
    *,
    actor: User,
    visitor_session_id: str | None,
    reported_name: str | None,
    reported_phone: str | None,
    reason: str,
    notes: str | None,
    severity: str,
) -> dict[str, Any]:
    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    window_start = utc_now() - timedelta(days=1)
    reports_today = (
        db.query(func.count(VisitorReport.id))
        .filter(VisitorReport.reporter_user_id == actor.id, VisitorReport.created_at >= window_start)
        .scalar()
        or 0
    )
    if reports_today >= MAX_VISITOR_REPORTS_PER_DAY:
        raise AppException("Daily visitor report limit reached. Please contact estate admin for urgent cases.", status_code=429)

    session = None
    if visitor_session_id:
        session = db.query(VisitorSession).filter(VisitorSession.id == visitor_session_id).first()
        if not session:
            raise AppException("Visitor record not found.", status_code=404)
        if session.estate_id != estate.id:
            raise AppException("Visitor record does not belong to your estate.", status_code=403)

    try:
        normalized_severity = VisitorReportSeverity(str(severity or "medium").strip().lower())
    except Exception as exc:
        raise AppException("Unsupported report severity.", status_code=400) from exc

    final_name = (reported_name or (session.visitor_label if session else "")).strip()
    if not final_name:
        raise AppException("Visitor name is required.", status_code=400)
    final_phone = (reported_phone or (session.visitor_phone if session else "")).strip() or None

    normalized_name = _normalize_name(final_name)
    normalized_phone = _normalize_phone(final_phone)
    watchlist = (
        db.query(WatchlistEntry)
        .filter(
            WatchlistEntry.estate_id == estate.id,
            WatchlistEntry.normalized_name == normalized_name,
            WatchlistEntry.normalized_phone == (normalized_phone or None),
        )
        .first()
    )

    report = VisitorReport(
        estate_id=estate.id,
        visitor_session_id=session.id if session else None,
        reporter_user_id=actor.id,
        host_user_id=session.homeowner_id if session else None,
        reported_name=final_name,
        reported_phone=final_phone,
        reason=(reason or "").strip(),
        notes=(notes or "").strip() or None,
        severity=normalized_severity,
        status=VisitorReportStatus.pending_review,
    )
    if not report.reason:
        raise AppException("Reason for report is required.", status_code=400)
    db.add(report)
    db.flush()

    if watchlist:
        watchlist.display_name = final_name
        watchlist.display_phone = final_phone
        watchlist.report_count = int(watchlist.report_count or 0) + 1
        watchlist.latest_report_id = report.id
        watchlist.last_reported_at = utc_now()
        watchlist.auto_flagged = bool(watchlist.report_count >= 2 or normalized_severity == VisitorReportSeverity.critical)
        watchlist.risk_level = _risk_for_report_count(watchlist.report_count, normalized_severity)
        report.occurrence_count = int(watchlist.report_count or 1)
    else:
        watchlist = WatchlistEntry(
            estate_id=estate.id,
            latest_report_id=report.id,
            display_name=final_name,
            display_phone=final_phone,
            normalized_name=normalized_name,
            normalized_phone=normalized_phone or None,
            report_count=1,
            auto_flagged=normalized_severity in {VisitorReportSeverity.high, VisitorReportSeverity.critical},
            risk_level=_risk_for_report_count(1, normalized_severity),
            last_reported_at=utc_now(),
        )
        db.add(watchlist)

    _log_audit(
        db,
        actor_user_id=actor.id,
        action="visitor_report.created",
        resource_type="visitor_report",
        resource_id=report.id,
        meta={
            "estateId": estate.id,
            "visitorSessionId": session.id if session else None,
            "watchlistEntryId": watchlist.id,
            "severity": normalized_severity.value,
        },
    )
    db.commit()
    db.refresh(report)
    db.refresh(watchlist)

    recipients = _list_security_users(db, estate.id)
    for recipient in recipients:
        create_notification(
            db=db,
            user_id=recipient.id,
            kind="safety.visitor_report",
            payload={
                "reportId": report.id,
                "estateId": estate.id,
                "reportedName": final_name,
                "severity": normalized_severity.value,
                "watchlistEntryId": watchlist.id,
                "message": f"Visitor report submitted for {final_name}. Review watchlist activity.",
            },
        )

    report_payload = serialize_visitor_report(report)
    watchlist_payload = get_watchlist(db, actor=actor, limit=20)
    _emit("safety.report.created", report_payload, rooms=[f"estate:{estate.id}:safety"])
    _emit("safety.watchlist.updated", {"items": watchlist_payload}, rooms=[f"estate:{estate.id}:safety"])
    return {
        "report": report_payload,
        "watchlistEntry": serialize_watchlist_entry(
            watchlist,
            recent_reports=[
                row
                for row in db.query(VisitorReport)
                .filter(
                    VisitorReport.estate_id == estate.id,
                    VisitorReport.reported_name == watchlist.display_name,
                )
                .order_by(VisitorReport.created_at.desc())
                .limit(5)
                .all()
            ],
        ),
    }


def get_watchlist(db: Session, *, actor: User, limit: int = 30) -> list[dict[str, Any]]:
    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    entries = (
        db.query(WatchlistEntry)
        .filter(WatchlistEntry.estate_id == estate.id)
        .order_by(WatchlistEntry.last_reported_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    items: list[dict[str, Any]] = []
    for entry in entries:
        reports = (
            db.query(VisitorReport)
            .filter(VisitorReport.estate_id == estate.id, VisitorReport.reported_name == entry.display_name)
            .order_by(VisitorReport.created_at.desc())
            .limit(5)
            .all()
        )
        items.append(serialize_watchlist_entry(entry, recent_reports=reports))
    return items


def get_safety_dashboard(db: Session, *, actor: User) -> dict[str, Any]:
    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    alerts = list_emergency_alerts(db, actor=actor, limit=20)
    reports_query = db.query(VisitorReport).filter(VisitorReport.estate_id == estate.id)
    if actor.role == UserRole.homeowner:
        reports_query = reports_query.filter(VisitorReport.host_user_id == actor.id)
    reports = reports_query.order_by(VisitorReport.created_at.desc()).limit(20).all()
    watchlist = get_watchlist(db, actor=actor, limit=12)
    active_alerts = [row for row in alerts if row["status"] not in {"resolved", "cancelled"}]
    return {
        "context": {
            "estateId": estate.id,
            "estateName": estate.name,
            "unitLabel": context["unitLabel"],
            "role": actor.role.value,
        },
        "metrics": {
            "activeAlerts": len(active_alerts),
            "criticalAlerts": len([row for row in active_alerts if row["priority"] == "critical"]),
            "watchlistCount": len(watchlist),
            "pendingReports": len([row for row in reports if row.status == VisitorReportStatus.pending_review]),
        },
        "alerts": alerts,
        "reports": [serialize_visitor_report(row) for row in reports],
        "watchlist": watchlist,
        "architecture": {
            "delivery": [
                "Primary: realtime dashboard/socket delivery over internet",
                "Secondary: queued SMS fallback events when SMS fallback is enabled",
                "Offline: trigger stored with offlineQueued flag and replay-safe request flow",
            ],
            "liability": "QRing facilitates emergency alerting and coordination. It does not guarantee police, fire, or medical response.",
        },
    }
