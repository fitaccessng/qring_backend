from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Door, Estate, Home, User
from app.db.models import HomeownerSetting
from app.services.payment_service import get_effective_subscription

logger = logging.getLogger(__name__)
DEFAULT_PANIC_SCHEDULE = []


def get_or_create_homeowner_settings(db: Session, user_id: str) -> HomeownerSetting:
    row = db.query(HomeownerSetting).filter(HomeownerSetting.user_id == user_id).first()
    if row:
        return row

    row = HomeownerSetting(user_id=user_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_homeowner_settings_payload(db: Session, user_id: str) -> dict:
    row = get_or_create_homeowner_settings(db, user_id)
    user = db.query(User).filter(User.id == user_id).first()
    primary_home = (
        db.query(Home)
        .filter(Home.homeowner_id == user_id)
        .order_by(Home.created_at.asc())
        .first()
    )
    door_count = (
        db.query(func.count(Door.id))
        .select_from(Door)
        .join(Home, Home.id == Door.home_id)
        .filter(Home.homeowner_id == user_id)
        .scalar()
        or 0
    )
    estate_row = None
    try:
        estate_row = (
            db.query(Home, Estate)
            .join(Estate, Estate.id == Home.estate_id)
            .filter(Home.homeowner_id == user_id, Home.estate_id.is_not(None))
            .order_by(Home.created_at.desc())
            .first()
        )
    except SQLAlchemyError:
        # Production schema drift (missing tables/columns) should not break the homeowner UI.
        # The exception will still be captured by app logs for follow-up.
        logger.exception("homeowner_settings_estate_lookup_failed user_id=%s", user_id)
        estate_row = None
    managed_by_estate = bool(estate_row)
    subscription_owner_id = estate_row[1].owner_id if estate_row else user_id
    subscription = get_effective_subscription(db, subscription_owner_id)
    return {
        "pushAlerts": row.push_alerts,
        "soundAlerts": row.sound_alerts,
        "autoRejectUnknownVisitors": row.auto_reject_unknown_visitors,
        "autoApproveTrustedVisitors": bool(row.auto_approve_trusted_visitors),
        "autoApproveKnownContacts": bool(row.auto_approve_known_contacts),
        "knownContacts": _parse_known_contacts(row.known_contacts_json),
        "allowDeliveryDropAtGate": bool(row.allow_delivery_drop_at_gate),
        "smsFallbackEnabled": bool(row.sms_fallback_enabled),
        "nearbyPanicAlertsEnabled": bool(getattr(row, "nearby_panic_alerts_enabled", True)),
        "nearbyPanicAlertRadiusMeters": int(getattr(row, "nearby_panic_alert_radius_m", 500) or 500),
        "nearbyPanicAvailability": str(getattr(row, "nearby_panic_availability_mode", "always") or "always"),
        "nearbyPanicCustomSchedule": _parse_json_list(getattr(row, "nearby_panic_schedule_json", "[]")),
        "nearbyPanicReceiveFrom": str(getattr(row, "nearby_panic_receive_from", "everyone") or "everyone"),
        "nearbyPanicMutedUntil": _to_iso(getattr(row, "nearby_panic_muted_until", None)),
        "nearbyPanicSameAreaLabel": getattr(row, "nearby_panic_same_area_label", None),
        "panicIdentityVisibility": str(getattr(row, "panic_identity_visibility", "masked") or "masked"),
        "safetyHomeLocation": {
            "lat": getattr(row, "safety_home_lat", None),
            "lng": getattr(row, "safety_home_lng", None),
        },
        "managedByEstate": managed_by_estate,
        "estateId": estate_row[1].id if estate_row else None,
        "estateName": estate_row[1].name if estate_row else None,
        "subscription": subscription,
        "profile": {
            "id": user.id if user else user_id,
            "fullName": user.full_name if user else "",
            "email": user.email if user else "",
            "phone": user.phone if user else None,
            "role": user.role.value if user and hasattr(user.role, "value") else (str(user.role) if user else "homeowner"),
            "securityLevel": "Estate Linked" if managed_by_estate else "Platinum",
        },
        "home": {
            "id": primary_home.id if primary_home else None,
            "name": primary_home.name if primary_home else None,
            "doorCount": int(door_count or 0),
        },
    }


def _parse_known_contacts(raw: str | None) -> list[str]:
    try:
        rows = [str(item or "").strip() for item in json.loads(raw or "[]")]
    except Exception:
        rows = []
    return [row for row in rows if row]


def _parse_json_list(raw: str | None) -> list[dict]:
    try:
        rows = json.loads(raw or "[]")
    except Exception:
        rows = []
    if not isinstance(rows, list):
        return list(DEFAULT_PANIC_SCHEDULE)
    return [row for row in rows if isinstance(row, dict)]


def _to_iso(value: datetime | None) -> str | None:
    if not value:
        return None
    try:
        return value.isoformat()
    except Exception:
        return None


def update_homeowner_settings(
    db: Session,
    user_id: str,
    push_alerts: bool,
    sound_alerts: bool,
    auto_reject_unknown_visitors: bool,
    auto_approve_trusted_visitors: bool = False,
    auto_approve_known_contacts: bool = False,
    known_contacts: list[str] | None = None,
    allow_delivery_drop_at_gate: bool = True,
    sms_fallback_enabled: bool = False,
    nearby_panic_alerts_enabled: bool = True,
    nearby_panic_alert_radius_m: int = 500,
    nearby_panic_availability_mode: str = "always",
    nearby_panic_schedule: list[dict] | None = None,
    nearby_panic_receive_from: str = "everyone",
    nearby_panic_muted_until: datetime | None = None,
    nearby_panic_same_area_label: str | None = None,
    panic_identity_visibility: str = "masked",
    safety_home_lat: float | None = None,
    safety_home_lng: float | None = None,
) -> dict:
    row = get_or_create_homeowner_settings(db, user_id)
    row.push_alerts = push_alerts
    row.sound_alerts = sound_alerts
    row.auto_reject_unknown_visitors = auto_reject_unknown_visitors
    row.auto_approve_trusted_visitors = auto_approve_trusted_visitors
    row.auto_approve_known_contacts = auto_approve_known_contacts
    row.known_contacts_json = json.dumps([str(item or "").strip() for item in (known_contacts or []) if str(item or "").strip()])
    row.allow_delivery_drop_at_gate = allow_delivery_drop_at_gate
    row.sms_fallback_enabled = sms_fallback_enabled
    row.nearby_panic_alerts_enabled = bool(nearby_panic_alerts_enabled)
    row.nearby_panic_alert_radius_m = max(200, min(int(nearby_panic_alert_radius_m or 500), 1000))
    normalized_availability = str(nearby_panic_availability_mode or "always").strip().lower()
    row.nearby_panic_availability_mode = normalized_availability if normalized_availability in {"always", "night_only", "custom"} else "always"
    row.nearby_panic_schedule_json = json.dumps(
        [item for item in (nearby_panic_schedule or DEFAULT_PANIC_SCHEDULE) if isinstance(item, dict)],
        ensure_ascii=True,
    )
    normalized_receive_from = str(nearby_panic_receive_from or "everyone").strip().lower()
    row.nearby_panic_receive_from = (
        normalized_receive_from if normalized_receive_from in {"everyone", "verified_only", "same_area"} else "everyone"
    )
    row.nearby_panic_muted_until = nearby_panic_muted_until
    row.nearby_panic_same_area_label = (nearby_panic_same_area_label or "").strip() or None
    normalized_visibility = str(panic_identity_visibility or "masked").strip().lower()
    row.panic_identity_visibility = normalized_visibility if normalized_visibility in {"masked", "public"} else "masked"
    row.safety_home_lat = safety_home_lat
    row.safety_home_lng = safety_home_lng
    db.commit()
    db.refresh(row)

    return {
        "pushAlerts": row.push_alerts,
        "soundAlerts": row.sound_alerts,
        "autoRejectUnknownVisitors": row.auto_reject_unknown_visitors,
        "autoApproveTrustedVisitors": bool(row.auto_approve_trusted_visitors),
        "autoApproveKnownContacts": bool(row.auto_approve_known_contacts),
        "knownContacts": _parse_known_contacts(row.known_contacts_json),
        "allowDeliveryDropAtGate": bool(row.allow_delivery_drop_at_gate),
        "smsFallbackEnabled": bool(row.sms_fallback_enabled),
        "nearbyPanicAlertsEnabled": bool(row.nearby_panic_alerts_enabled),
        "nearbyPanicAlertRadiusMeters": int(row.nearby_panic_alert_radius_m or 500),
        "nearbyPanicAvailability": str(row.nearby_panic_availability_mode or "always"),
        "nearbyPanicCustomSchedule": _parse_json_list(row.nearby_panic_schedule_json),
        "nearbyPanicReceiveFrom": str(row.nearby_panic_receive_from or "everyone"),
        "nearbyPanicMutedUntil": _to_iso(row.nearby_panic_muted_until),
        "nearbyPanicSameAreaLabel": row.nearby_panic_same_area_label,
        "panicIdentityVisibility": str(row.panic_identity_visibility or "masked"),
        "safetyHomeLocation": {
            "lat": row.safety_home_lat,
            "lng": row.safety_home_lng,
        },
    }
