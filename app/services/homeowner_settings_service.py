from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Estate, Home
from app.db.models import HomeownerSetting
from app.services.payment_service import get_effective_subscription


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
    estate_row = (
        db.query(Home, Estate)
        .join(Estate, Estate.id == Home.estate_id)
        .filter(Home.homeowner_id == user_id, Home.estate_id.is_not(None))
        .order_by(Home.created_at.desc())
        .first()
    )
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
        "managedByEstate": managed_by_estate,
        "estateId": estate_row[1].id if estate_row else None,
        "estateName": estate_row[1].name if estate_row else None,
        "subscription": subscription,
    }


def _parse_known_contacts(raw: str | None) -> list[str]:
    try:
        rows = [str(item or "").strip() for item in __import__("json").loads(raw or "[]")]
    except Exception:
        rows = []
    return [row for row in rows if row]


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
) -> dict:
    row = get_or_create_homeowner_settings(db, user_id)
    row.push_alerts = push_alerts
    row.sound_alerts = sound_alerts
    row.auto_reject_unknown_visitors = auto_reject_unknown_visitors
    row.auto_approve_trusted_visitors = auto_approve_trusted_visitors
    row.auto_approve_known_contacts = auto_approve_known_contacts
    row.known_contacts_json = __import__("json").dumps([str(item or "").strip() for item in (known_contacts or []) if str(item or "").strip()])
    row.allow_delivery_drop_at_gate = allow_delivery_drop_at_gate
    row.sms_fallback_enabled = sms_fallback_enabled
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
    }
