from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from typing import Any

from app.core.time import ensure_utc, utc_now
from app.db.models import SubscriptionEvent


WARNING_SCHEDULE_DAYS = {14, 10, 7, 5, 3, 1}


def _coerce_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except Exception:
        return None


def _days_until(target: datetime | None, *, now: datetime) -> int | None:
    if not target:
        return None
    delta = target - now
    return max(0, int((delta.total_seconds() + 86399) // 86400))


def resolve_status(subscription: Any, *, now: datetime | None = None) -> str:
    current_time = ensure_utc(now) or utc_now()
    trial_ends_at = _coerce_datetime(getattr(subscription, "trial_ends_at", None))
    ends_at = _coerce_datetime(getattr(subscription, "ends_at", None))
    grace_ends_at = _coerce_datetime(getattr(subscription, "grace_ends_at", None))
    payment_status = str(getattr(subscription, "payment_status", "") or "").strip().lower()

    if payment_status == "pending":
        return "payment_pending"
    if trial_ends_at and trial_ends_at > current_time:
        return "trial"
    if ends_at and ends_at > current_time:
        days_to_expiry = _days_until(ends_at, now=current_time)
        if days_to_expiry is not None and days_to_expiry <= 14:
            return "expiring_soon"
        return "active"
    if grace_ends_at and grace_ends_at > current_time:
        return "grace_period"
    if ends_at and ends_at <= current_time:
        return "suspended"
    return str(getattr(subscription, "status", "inactive") or "inactive").strip().lower()


def compute_warning_phase(subscription: Any, *, now: datetime | None = None) -> str | None:
    current_time = ensure_utc(now) or utc_now()
    ends_at = _coerce_datetime(getattr(subscription, "ends_at", None))
    days_to_expiry = _days_until(ends_at, now=current_time)
    if days_to_expiry is None or days_to_expiry > 14:
        return None
    if days_to_expiry <= 4:
        return "high"
    if days_to_expiry <= 9:
        return "medium"
    return "soft"


def compute_allowed_actions(subscription: Any, *, actor_role: str, is_bill_payer: bool) -> dict[str, bool]:
    status = resolve_status(subscription)
    base = {
        "view_dashboard": True,
        "view_logs": True,
        "view_messages": True,
        "renew_subscription": True,
        "manage_billing": is_bill_payer,
        "respond_to_visitor": True,
        "scan_qr": True,
        "approve_entry": True,
        "deny_entry": True,
        "start_call": True,
        "send_message": True,
        "create_qr": True,
        "create_visitor_request": True,
        "add_home": True,
        "add_user": True,
        "edit_settings": True,
        "edit_automation": True,
        "send_broadcast": True,
        "export_reports": True,
    }

    if status == "grace_period":
        for action_key in ("add_home", "add_user", "edit_settings", "edit_automation", "send_broadcast", "export_reports"):
            base[action_key] = False
    elif status == "suspended":
        for action_key in list(base.keys()):
            if action_key not in {"view_dashboard", "view_logs", "view_messages", "renew_subscription", "manage_billing"}:
                base[action_key] = False

    if actor_role == "security":
        base["manage_billing"] = False
        base["add_home"] = False
        base["add_user"] = False

    return base


def build_subscription_summary(subscription: Any, *, actor_role: str, is_bill_payer: bool, now: datetime | None = None) -> dict[str, Any]:
    current_time = ensure_utc(now) or utc_now()
    ends_at = _coerce_datetime(getattr(subscription, "ends_at", None))
    grace_ends_at = _coerce_datetime(getattr(subscription, "grace_ends_at", None))

    return {
        "plan": getattr(subscription, "plan", None),
        "status": resolve_status(subscription, now=current_time),
        "days_to_expiry": _days_until(ends_at, now=current_time),
        "grace_days_left": _days_until(grace_ends_at, now=current_time) or 0,
        "is_bill_payer": is_bill_payer,
        "warning_phase": compute_warning_phase(subscription, now=current_time),
        "allowed_actions": compute_allowed_actions(subscription, actor_role=actor_role, is_bill_payer=is_bill_payer),
        "current_period_end": ends_at.isoformat() if ends_at else None,
        "grace_ends_at": grace_ends_at.isoformat() if grace_ends_at else None,
        "billing_scope": getattr(subscription, "billing_scope", actor_role),
    }


def should_send_warning(subscription: Any, *, now: datetime | None = None) -> bool:
    current_time = ensure_utc(now) or utc_now()
    ends_at = _coerce_datetime(getattr(subscription, "ends_at", None))
    days_to_expiry = _days_until(ends_at, now=current_time)
    return days_to_expiry in WARNING_SCHEDULE_DAYS


def serialize_subscription_metadata(metadata: dict[str, Any] | None = None) -> str:
    try:
        return json.dumps(metadata or {})
    except Exception:
        return "{}"


def create_subscription_event(
    *,
    subscription_id: str,
    event_type: str,
    old_status: str | None = None,
    new_status: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SubscriptionEvent:
    return SubscriptionEvent(
        subscription_id=subscription_id,
        event_type=event_type,
        old_status=old_status,
        new_status=new_status,
        metadata_json=serialize_subscription_metadata(metadata),
    )


def sync_subscription_lifecycle(
    subscription: Any,
    *,
    now: datetime | None = None,
    default_grace_days: int = 5,
) -> dict[str, Any]:
    current_time = ensure_utc(now) or utc_now()
    previous_status = str(getattr(subscription, "status", "") or "").strip().lower() or None
    grace_days = int(getattr(subscription, "grace_days", None) or default_grace_days)
    expires_at = _coerce_datetime(getattr(subscription, "ends_at", None)) or _coerce_datetime(
        getattr(subscription, "trial_ends_at", None)
    )
    grace_ends_at = _coerce_datetime(getattr(subscription, "grace_ends_at", None))

    if expires_at and not grace_ends_at:
        grace_ends_at = expires_at + timedelta(days=grace_days)
        setattr(subscription, "grace_ends_at", grace_ends_at)

    if not expires_at:
        return {
            "previous_status": previous_status,
            "status": previous_status,
            "status_changed": False,
            "expires_at": None,
            "grace_ends_at": grace_ends_at,
            "warning_phase": compute_warning_phase(subscription, now=current_time),
        }

    if expires_at > current_time:
        next_status = "expiring_soon" if 0 <= (expires_at - current_time).days <= 14 else "active"
        warning_phase = compute_warning_phase(subscription, now=current_time)
        suspension_reason = None
    elif grace_ends_at and current_time < grace_ends_at:
        next_status = "grace_period"
        warning_phase = "high"
        suspension_reason = None
    else:
        next_status = "suspended"
        warning_phase = "high"
        suspension_reason = str(getattr(subscription, "suspension_reason", "") or "").strip() or "non_payment"

    setattr(subscription, "status", next_status)
    setattr(subscription, "warning_phase", warning_phase)
    setattr(subscription, "suspension_reason", suspension_reason)

    payment_status = str(getattr(subscription, "payment_status", "") or "").strip().lower()
    if next_status in {"grace_period", "suspended"} and payment_status not in {"paid", "free", "expired"}:
        setattr(subscription, "payment_status", "expired")

    return {
        "previous_status": previous_status,
        "status": next_status,
        "status_changed": previous_status != next_status,
        "expires_at": expires_at,
        "grace_ends_at": grace_ends_at,
        "warning_phase": warning_phase,
    }
