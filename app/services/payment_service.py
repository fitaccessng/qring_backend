from __future__ import annotations

import json
import hmac
import uuid
import re
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha512
from typing import Any
from urllib.parse import urlparse
from urllib import error, request

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.time import ensure_utc, utc_now
from app.db.models import (
    Estate,
    Home,
    Notification,
    PaymentAttempt,
    PaymentPurpose,
    ReferralReward,
    Subscription,
    SubscriptionInvoice,
    SubscriptionPlan,
    User,
)
from app.services.subscription_policy_service import (
    build_subscription_summary,
    create_subscription_event,
    serialize_subscription_metadata,
    sync_subscription_lifecycle,
)

settings = get_settings()
REFERRAL_REWARD_AMOUNT = 2000
DEFAULT_GRACE_DAYS = 5
SIGNUP_TRIAL_DAYS = 30

DEFAULT_PLAN_CATALOG = [
    {
        "id": "estate_starter",
        "name": "Starter Estate",
        "amount": 0,
        "currency": "NGN",
        "billingLabel": "month",
        "maxEstates": 1,
        "maxHomes": 3,
        "maxDoors": 3,
        "maxQrCodes": 3,
        "maxAdmins": 1,
        "active": True,
        "audience": "estate",
        "durationDays": 30,
        "trialDays": 30,
        "selfServe": True,
        "description": "Up to 3 houses. Full system access at limited scale for 30 days.",
        "enabledFeatures": [
            "manual_visitor_logging",
            "basic_notifications",
            "basic_dashboard",
            "approve_reject_visitor_access",
            "limited_logs",
            "realtime_alerts",
            "visitor_logs",
            "resident_management",
            "mobile_dashboard",
            "chat_call_verification",
            "multi_admin_roles",
            "visitor_scheduling",
            "access_time_windows",
            "analytics",
            "activity_tracking",
            "advanced_analytics",
            "security_audit_logs",
            "role_permissions",
        ],
        "restrictions": [
            "multi_location_control",
            "priority_support",
            "sla_support",
            "api_access",
        ],
    },
    {
        "id": "estate_basic",
        "name": "Estate Basic",
        "amount": 6000,
        "currency": "NGN",
        "billingLabel": "month",
        "maxEstates": 1,
        "maxHomes": 10,
        "maxDoors": 10,
        "maxQrCodes": 10,
        "maxAdmins": 1,
        "active": True,
        "audience": "estate",
        "durationDays": 30,
        "selfServe": True,
        "description": "Up to 10 houses with realtime alerts, visitor logs, resident management, and mobile dashboard.",
        "enabledFeatures": [
            "manual_visitor_logging",
            "basic_notifications",
            "basic_dashboard",
            "approve_reject_visitor_access",
            "limited_logs",
            "realtime_alerts",
            "visitor_logs",
            "resident_management",
            "mobile_dashboard",
        ],
        "restrictions": [
            "visitor_scheduling",
            "chat_call_verification",
            "multi_admin_roles",
            "analytics",
            "activity_tracking",
            "advanced_analytics",
            "access_time_windows",
            "security_audit_logs",
            "role_permissions",
            "priority_support",
            "multi_location_control",
            "sla_support",
            "api_access",
        ],
    },
    {
        "id": "estate_plus",
        "name": "Estate Plus",
        "amount": 9000,
        "currency": "NGN",
        "billingLabel": "month",
        "maxEstates": 1,
        "maxHomes": 15,
        "maxDoors": 15,
        "maxQrCodes": 15,
        "maxAdmins": 2,
        "active": True,
        "audience": "estate",
        "durationDays": 30,
        "selfServe": True,
        "description": "Everything in Basic plus visitor scheduling, access time windows, and chat + call verification.",
        "enabledFeatures": [
            "manual_visitor_logging",
            "basic_notifications",
            "basic_dashboard",
            "approve_reject_visitor_access",
            "limited_logs",
            "realtime_alerts",
            "visitor_logs",
            "resident_management",
            "mobile_dashboard",
            "visitor_scheduling",
            "access_time_windows",
            "chat_call_verification",
        ],
        "restrictions": [
            "multi_admin_roles",
            "analytics",
            "activity_tracking",
            "advanced_analytics",
            "security_audit_logs",
            "role_permissions",
            "priority_support",
            "multi_location_control",
            "sla_support",
            "api_access",
        ],
    },
    {
        "id": "estate_growth",
        "name": "Estate Growth",
        "amount": 18000,
        "currency": "NGN",
        "billingLabel": "month",
        "maxEstates": 2,
        "maxHomes": 30,
        "maxDoors": 30,
        "maxQrCodes": 30,
        "maxAdmins": 5,
        "active": True,
        "audience": "estate",
        "durationDays": 30,
        "selfServe": True,
        "description": "Everything in Plus with multi-admin roles, analytics dashboard, and activity tracking.",
        "enabledFeatures": [
            "manual_visitor_logging",
            "basic_notifications",
            "basic_dashboard",
            "approve_reject_visitor_access",
            "limited_logs",
            "realtime_alerts",
            "visitor_logs",
            "resident_management",
            "mobile_dashboard",
            "chat_call_verification",
            "multi_admin_roles",
            "visitor_scheduling",
            "access_time_windows",
            "analytics",
            "activity_tracking",
        ],
        "restrictions": [
            "advanced_analytics",
            "security_audit_logs",
            "multi_location_control",
            "role_permissions",
            "priority_support",
            "sla_support",
            "api_access",
        ],
    },
    {
        "id": "estate_pro",
        "name": "Estate Pro",
        "amount": 30000,
        "currency": "NGN",
        "billingLabel": "month",
        "maxEstates": 5,
        "maxHomes": 50,
        "maxDoors": 50,
        "maxQrCodes": 50,
        "maxAdmins": 15,
        "active": True,
        "audience": "estate",
        "durationDays": 30,
        "selfServe": True,
        "description": "Everything in Growth with advanced analytics, security audit logs, role permissions, and priority support.",
        "enabledFeatures": [
            "manual_visitor_logging",
            "basic_notifications",
            "basic_dashboard",
            "approve_reject_visitor_access",
            "limited_logs",
            "realtime_alerts",
            "visitor_logs",
            "resident_management",
            "mobile_dashboard",
            "chat_call_verification",
            "multi_admin_roles",
            "visitor_scheduling",
            "access_time_windows",
            "analytics",
            "activity_tracking",
            "advanced_analytics",
            "security_audit_logs",
            "role_permissions",
            "priority_support",
        ],
        "restrictions": ["multi_location_control", "api_access", "sla_support"],
    },
    {
        "id": "estate_enterprise",
        "name": "Enterprise Estate",
        "amount": 0,
        "currency": "NGN",
        "billingLabel": "custom",
        "maxEstates": 0,
        "maxHomes": 0,
        "maxDoors": 0,
        "maxQrCodes": 0,
        "maxAdmins": 0,
        "active": True,
        "audience": "estate",
        "durationDays": 365,
        "selfServe": False,
        "manualActivationRequired": True,
        "description": "Custom plan for large estates",
        "enabledFeatures": [
            "manual_visitor_logging",
            "basic_notifications",
            "basic_dashboard",
            "approve_reject_visitor_access",
            "limited_logs",
            "realtime_alerts",
            "visitor_logs",
            "resident_management",
            "mobile_dashboard",
            "chat_call_verification",
            "multi_admin_roles",
            "visitor_scheduling",
            "access_time_windows",
            "analytics",
            "activity_tracking",
            "advanced_analytics",
            "security_audit_logs",
            "multi_location_control",
            "role_permissions",
            "priority_support",
            "sla_support",
            "api_access",
        ],
        "restrictions": [],
    },
    {
        "id": "free",
        "name": "Free",
        "amount": 0,
        "currency": "NGN",
        "billingLabel": "month",
        "maxDoors": 1,
        "maxQrCodes": 1,
        "maxAdmins": 1,
        "active": True,
        "audience": "homeowner",
        "durationDays": None,
        "selfServe": True,
        "description": "1 door with basic notifications and limited logs.",
        "enabledFeatures": [
            "basic_notifications",
            "limited_logs",
        ],
        "restrictions": [
            "advanced_notifications",
            "visitor_scheduling",
            "multi_door_access",
            "chat_call_verification",
            "priority_support",
            "access_time_windows",
            "advanced_privacy_controls",
            "visitor_history",
        ],
    },
    {
        "id": "home_pro",
        "name": "Home Pro",
        "amount": 2500,
        "currency": "NGN",
        "billingLabel": "month",
        "maxDoors": 1,
        "maxQrCodes": 5,
        "maxAdmins": 1,
        "active": True,
        "audience": "homeowner",
        "durationDays": 30,
        "selfServe": True,
        "description": "Smart homeowner controls with chat + call verification, visitor history, scheduling, and advanced notifications.",
        "enabledFeatures": [
            "basic_notifications",
            "limited_logs",
            "chat_call_verification",
            "visitor_history",
            "visitor_scheduling",
            "advanced_notifications",
        ],
        "restrictions": [
            "multi_door_access",
            "access_time_windows",
            "priority_support",
            "advanced_privacy_controls",
        ],
    },
    {
        "id": "home_premium",
        "name": "Home Premium",
        "amount": 4500,
        "currency": "NGN",
        "billingLabel": "month",
        "maxDoors": 5,
        "maxQrCodes": 20,
        "maxAdmins": 1,
        "active": True,
        "audience": "homeowner",
        "durationDays": 30,
        "selfServe": True,
        "description": "Advanced access and privacy with multiple doors, access time windows, priority support, and advanced privacy controls.",
        "enabledFeatures": [
            "basic_notifications",
            "limited_logs",
            "chat_call_verification",
            "visitor_history",
            "visitor_scheduling",
            "advanced_notifications",
            "multi_door_access",
            "access_time_windows",
            "priority_support",
            "advanced_privacy_controls",
        ],
        "restrictions": [],
    },
    # Legacy plans retained for backwards compatibility with existing subscriptions.
    {"id": "doors_20", "name": "Legacy Basic Plan", "amount": 12000, "currency": "NGN", "maxDoors": 10, "maxQrCodes": 10, "active": True, "audience": "legacy", "selfServe": False, "hidden": True},
    {"id": "doors_40", "name": "Legacy Standard Plan", "amount": 25000, "currency": "NGN", "maxDoors": 22, "maxQrCodes": 22, "active": True, "audience": "legacy", "selfServe": False, "hidden": True},
    {"id": "doors_80", "name": "Legacy Pro Estate Plan", "amount": 50000, "currency": "NGN", "maxDoors": 46, "maxQrCodes": 46, "active": True, "audience": "legacy", "selfServe": False, "hidden": True},
    {"id": "doors_100", "name": "Legacy Premium Estate Plan", "amount": 100000, "currency": "NGN", "maxDoors": 100, "maxQrCodes": 100, "active": True, "audience": "legacy", "selfServe": False, "hidden": True},
]

ALL_FEATURE_FLAGS = {
    "manual_visitor_logging",
    "basic_notifications",
    "basic_dashboard",
    "approve_reject_visitor_access",
    "limited_logs",
    "realtime_alerts",
    "visitor_logs",
    "resident_management",
    "mobile_dashboard",
    "chat_call_verification",
    "multi_admin_roles",
    "visitor_scheduling",
    "access_time_windows",
    "analytics",
    "activity_tracking",
    "advanced_analytics",
    "security_audit_logs",
    "multi_location_control",
    "role_permissions",
    "priority_support",
    "sla_support",
    "api_access",
    "advanced_notifications",
    "visitor_history",
    "multi_door_access",
    "advanced_privacy_controls",
}

LIMITED_LOG_RETENTION_DAYS = 14
USAGE_WARNING_THRESHOLD = 0.8
FEATURE_LABELS = {
    "manual_visitor_logging": "manual visitor logging",
    "basic_notifications": "basic notifications",
    "basic_dashboard": "basic dashboard",
    "approve_reject_visitor_access": "visitor approval",
    "limited_logs": "limited logs",
    "realtime_alerts": "realtime alerts",
    "visitor_logs": "visitor logs",
    "resident_management": "resident management",
    "mobile_dashboard": "mobile dashboard",
    "chat_call_verification": "chat and call verification",
    "multi_admin_roles": "multi-admin roles",
    "visitor_scheduling": "visitor scheduling",
    "access_time_windows": "access time windows",
    "analytics": "analytics",
    "activity_tracking": "activity tracking",
    "advanced_analytics": "advanced analytics",
    "security_audit_logs": "security audit logs",
    "multi_location_control": "multi-location control",
    "role_permissions": "role permissions",
    "priority_support": "priority support",
    "sla_support": "SLA support",
    "api_access": "API access",
    "advanced_notifications": "advanced notifications",
    "visitor_history": "visitor history",
    "multi_door_access": "multiple door access",
    "advanced_privacy_controls": "advanced privacy controls",
}


def _normalize_url(value: str | None) -> str:
    return (value or "").strip().rstrip("/")


def _normalize_secret(value: str | None) -> str:
    return (value or "").strip()


def _is_public_https_url(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host not in {"", "localhost", "127.0.0.1"}


def _compute_expected_amount_kobo(plan_amount: int | float, billing_cycle: str | None) -> int:
    cycle = (billing_cycle or "monthly").strip().lower()
    if cycle not in {"monthly", "yearly"}:
        cycle = "monthly"
    cycle_multiplier = 12 if cycle == "yearly" else 1
    return int(plan_amount * cycle_multiplier * 100)


def _extract_paystack_error(detail: str) -> tuple[str | None, str]:
    fallback_message = (detail or "").strip()
    try:
        parsed = json.loads(detail)
    except Exception:
        return None, fallback_message

    code = parsed.get("code")
    message = parsed.get("message") or fallback_message
    if not code and isinstance(parsed.get("data"), dict):
        code = parsed["data"].get("code")
        message = parsed["data"].get("message") or message
    if code is not None:
        code = str(code).strip()
    if not code and re.search(r"(^|\\D)1010(\\D|$)", message):
        code = "1010"
    return code, str(message).strip()


def create_payment_purpose(db: Session, name: str, description: str, account_info: str):
    purpose = PaymentPurpose(name=name, description=description, account_info=account_info)
    db.add(purpose)
    db.commit()
    db.refresh(purpose)
    return purpose


def _catalog_row_by_id(plan_id: str) -> dict[str, Any]:
    return next((item for item in DEFAULT_PLAN_CATALOG if item["id"] == plan_id), {})


def _decode_json_list(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _encode_json_list(values: list[str] | None) -> str:
    return json.dumps([str(item).strip() for item in (values or []) if str(item).strip()])


def _is_user_in_signup_trial(user: User, *, now: datetime | None = None) -> bool:
    """Check if user is within 30 days of signup to grant all features."""
    if not user or not user.created_at:
        return False
    current_time = now or utc_now()
    created_at = ensure_utc(user.created_at)
    days_since_signup = (current_time - created_at).days
    return 0 <= days_since_signup < SIGNUP_TRIAL_DAYS


def _build_feature_flags(features: list[str], user: User | None = None, *, now: datetime | None = None) -> dict[str, bool]:
    # If user is within 30-day signup trial, grant all features
    if _is_user_in_signup_trial(user, now=now):
        return {feature: True for feature in sorted(ALL_FEATURE_FLAGS)}
    
    enabled = {str(item).strip() for item in (features or []) if str(item).strip()}
    return {feature: feature in enabled for feature in sorted(ALL_FEATURE_FLAGS)}


def _plan_payload(row: SubscriptionPlan, catalog_row: dict[str, Any], user: User | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    features = _decode_json_list(getattr(row, "enabled_features", "[]")) or list(catalog_row.get("enabledFeatures") or [])
    restrictions = _decode_json_list(getattr(row, "restrictions", "[]")) or list(catalog_row.get("restrictions") or [])
    monthly_amount = int(row.amount or 0)
    yearly_amount = monthly_amount * 12 if monthly_amount > 0 else 0
    return {
        "id": row.id,
        "name": row.name,
        "amount": monthly_amount,
        "monthlyAmount": monthly_amount,
        "yearlyAmount": yearly_amount,
        "currency": row.currency or "NGN",
        "billingLabel": catalog_row.get("billingLabel", "month"),
        "maxDoors": int(row.max_doors or 0),
        "maxQrCodes": int(row.max_qr_codes or 0),
        "maxAdmins": int(getattr(row, "max_admins", 1) or 1),
        "active": bool(row.active),
        "audience": getattr(row, "audience", None) or catalog_row.get("audience", "homeowner"),
        "trialDays": int(getattr(row, "trial_days", None) or catalog_row.get("trialDays", 0) or 0),
        "durationDays": getattr(row, "duration_days", None) if getattr(row, "duration_days", None) is not None else catalog_row.get("durationDays"),
        "selfServe": bool(getattr(row, "self_serve", None) if getattr(row, "self_serve", None) is not None else catalog_row.get("selfServe", True)),
        "manualActivationRequired": bool(
            getattr(row, "manual_activation_required", None)
            if getattr(row, "manual_activation_required", None) is not None
            else catalog_row.get("manualActivationRequired", False)
        ),
        "hidden": bool(getattr(row, "hidden", None) if getattr(row, "hidden", None) is not None else catalog_row.get("hidden", False)),
        "description": catalog_row.get("description", ""),
        "enabledFeatures": features,
        "restrictions": restrictions,
        "featureFlags": _build_feature_flags(features, user=user, now=now),
        "billingCycles": {
            "monthly": {
                "amount": monthly_amount,
                "label": "month",
            },
            "yearly": {
                "amount": yearly_amount,
                "label": "year",
            },
        },
    }


def _resolve_duration_days(plan_meta: dict[str, Any], billing_cycle: str | None) -> int | None:
    base_duration = plan_meta.get("durationDays")
    if base_duration in (None, 0):
        return None
    cycle = (billing_cycle or "monthly").strip().lower()
    if cycle == "yearly":
        return int(base_duration) * 12
    return int(base_duration)


def _insert_notification_if_missing(
    db: Session,
    *,
    user_id: str,
    kind: str,
    unique_key: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    recent = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.kind == kind)
        .order_by(Notification.created_at.desc())
        .limit(30)
        .all()
    )
    for row in recent:
        try:
            parsed = json.loads(row.payload or "{}")
        except Exception:
            parsed = {}
        if str(parsed.get("uniqueKey") or "").strip() == unique_key:
            return

    db.add(
        Notification(
            user_id=user_id,
            kind=kind,
            payload=json.dumps({"message": message, "uniqueKey": unique_key, **(payload or {})}),
        )
    )
    db.commit()


def _notify_trial_and_expiry_windows(db: Session, *, user_id: str, subscription: dict[str, Any]) -> None:
    if not subscription.get("isTrial"):
        return
    expires_at = subscription.get("expiresAt")
    if not expires_at:
        return
    try:
        expiry_dt = datetime.fromisoformat(str(expires_at))
    except Exception:
        return
    remaining_days = max((expiry_dt - utc_now()).days, 0)
    if 0 < remaining_days <= 3:
        _insert_notification_if_missing(
            db,
            user_id=user_id,
            kind="subscription.trial.expiring",
            unique_key=f"trial-expiring:{subscription.get('plan')}:{remaining_days}",
            message=f"Your {subscription.get('planName') or 'trial'} expires in {remaining_days} day(s). Upgrade to keep premium access.",
            payload={"plan": subscription.get("plan"), "expiresAt": expires_at, "daysRemaining": remaining_days},
        )
    if subscription.get("status") == "expired":
        _insert_notification_if_missing(
            db,
            user_id=user_id,
            kind="subscription.expired",
            unique_key=f"subscription-expired:{subscription.get('plan')}:{expires_at}",
            message=f"Your {subscription.get('planName') or 'subscription'} has expired. Upgrade to restore access.",
            payload={"plan": subscription.get("plan"), "expiresAt": expires_at},
        )


def _default_plan_id_for_audience(audience: str) -> str:
    return "estate_starter" if audience == "estate" else "free"


def _resolve_subscription_scope(user: User | None, audience: str) -> tuple[str, str, str]:
    tenant_type = audience or "homeowner"
    tenant_id = str(user.id) if user else ""
    billing_scope = "estate" if tenant_type == "estate" else "homeowner"
    return tenant_type, tenant_id, billing_scope


def _apply_subscription_lifecycle(row: Subscription, *, now: datetime | None = None) -> Subscription:
    current_time = now or utc_now()
    lifecycle = sync_subscription_lifecycle(row, now=current_time, default_grace_days=DEFAULT_GRACE_DAYS)
    if lifecycle["status_changed"]:
        event_type = "subscription.entered_grace" if row.status == "grace_period" else "subscription.suspended" if row.status == "suspended" else "subscription.expiring_soon"
        try:
            from sqlalchemy.orm.session import object_session

            session = object_session(row)
            if session is not None and getattr(row, "id", None):
                _log_subscription_event(
                    session,
                    subscription_id=row.id,
                    event_type=event_type,
                    old_status=lifecycle["previous_status"],
                    new_status=row.status,
                    metadata={"expiresAt": lifecycle["expires_at"].isoformat() if lifecycle["expires_at"] else None},
                )
        except Exception:
            pass
    return row


def _merge_subscription_summary(base: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    return {
        **base,
        "status": summary.get("status", base.get("status")),
        "daysToExpiry": summary.get("days_to_expiry"),
        "days_to_expiry": summary.get("days_to_expiry"),
        "graceDaysLeft": summary.get("grace_days_left", 0),
        "grace_days_left": summary.get("grace_days_left", 0),
        "isBillPayer": summary.get("is_bill_payer", False),
        "is_bill_payer": summary.get("is_bill_payer", False),
        "warningPhase": summary.get("warning_phase"),
        "warning_phase": summary.get("warning_phase"),
        "allowedActions": summary.get("allowed_actions", {}),
        "allowed_actions": summary.get("allowed_actions", {}),
        "currentPeriodEnd": summary.get("current_period_end"),
        "current_period_end": summary.get("current_period_end"),
        "graceEndsAt": summary.get("grace_ends_at"),
        "grace_ends_at": summary.get("grace_ends_at"),
        "billingScope": summary.get("billing_scope"),
        "billing_scope": summary.get("billing_scope"),
    }


def _to_decimal_amount(amount: int | float | str | Decimal | None, *, kobo: bool = False) -> Decimal:
    if amount in (None, ""):
        value = Decimal("0")
    else:
        value = Decimal(str(amount))
    return (value / Decimal("100")) if kobo else value


def _serialize_json(value: dict[str, Any] | list[Any] | None) -> str:
    if isinstance(value, list):
        try:
            return json.dumps(value)
        except Exception:
            return "[]"
    return serialize_subscription_metadata(value)


def _log_subscription_event(
    db: Session,
    *,
    subscription_id: str,
    event_type: str,
    old_status: str | None = None,
    new_status: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        create_subscription_event(
            subscription_id=subscription_id,
            event_type=event_type,
            old_status=old_status,
            new_status=new_status,
            metadata=metadata,
        )
    )


def _ensure_subscription_billing_record(
    db: Session,
    *,
    user_id: str,
    plan_id: str,
    billing_cycle: str,
) -> Subscription:
    row = get_user_subscription(db, user_id)
    if row:
        return row

    user = db.query(User).filter(User.id == user_id).first()
    plan_meta = get_plan_or_raise(db, plan_id, include_inactive=True)
    tenant_type, tenant_id, billing_scope = _resolve_subscription_scope(user, str(plan_meta.get("audience") or "homeowner"))
    current_time = utc_now()
    row = Subscription(
        user_id=user_id,
        plan=plan_id,
        status="payment_pending",
        payment_status="pending",
        billing_cycle=billing_cycle,
        tenant_type=tenant_type,
        tenant_id=tenant_id,
        billing_scope=billing_scope,
        auto_renew=True,
        cancel_at_period_end=False,
        grace_days=DEFAULT_GRACE_DAYS,
        last_payment_attempt_at=current_time,
        amount_due=plan_meta.get("amount") or 0,
        amount_paid=0,
        timezone="Africa/Lagos",
        starts_at=current_time,
    )
    db.add(row)
    db.flush()
    _log_subscription_event(
        db,
        subscription_id=row.id,
        event_type="subscription.payment_pending",
        old_status=None,
        new_status=row.status,
        metadata={"planId": plan_id, "billingCycle": billing_cycle, "source": "paystack_initialize"},
    )
    return row


def _find_invoice_by_reference(db: Session, reference: str | None) -> SubscriptionInvoice | None:
    if not reference:
        return None
    return (
        db.query(SubscriptionInvoice)
        .filter(SubscriptionInvoice.provider == "paystack", SubscriptionInvoice.provider_reference == str(reference))
        .order_by(SubscriptionInvoice.created_at.desc())
        .first()
    )


def _create_or_update_invoice_and_attempt(
    db: Session,
    *,
    subscription: Subscription,
    reference: str,
    amount_kobo: int,
    currency: str,
    billing_cycle: str,
    callback_url: str | None,
    payload: dict[str, Any],
) -> tuple[SubscriptionInvoice, PaymentAttempt]:
    invoice = _find_invoice_by_reference(db, reference)
    if not invoice:
        invoice = SubscriptionInvoice(
            subscription_id=subscription.id,
            provider="paystack",
            provider_reference=reference,
            amount_expected=_to_decimal_amount(amount_kobo, kobo=True),
            amount_received=Decimal("0"),
            currency=currency,
            status="pending",
            due_at=utc_now(),
            raw_payload=_serialize_json(payload),
        )
        db.add(invoice)
        db.flush()
    else:
        invoice.subscription_id = subscription.id
        invoice.amount_expected = _to_decimal_amount(amount_kobo, kobo=True)
        invoice.currency = currency
        invoice.status = "pending"
        invoice.raw_payload = _serialize_json(payload)

    attempt = (
        db.query(PaymentAttempt)
        .filter(PaymentAttempt.provider == "paystack", PaymentAttempt.provider_reference == reference)
        .order_by(PaymentAttempt.attempted_at.desc())
        .first()
    )
    if not attempt:
        attempt = PaymentAttempt(
            subscription_id=subscription.id,
            invoice_id=invoice.id,
            provider="paystack",
            provider_reference=reference,
            status="pending",
            amount=_to_decimal_amount(amount_kobo, kobo=True),
            attempted_at=utc_now(),
        )
        db.add(attempt)
    else:
        attempt.subscription_id = subscription.id
        attempt.invoice_id = invoice.id
        attempt.status = "pending"
        attempt.amount = _to_decimal_amount(amount_kobo, kobo=True)

    subscription.last_payment_attempt_at = utc_now()
    subscription.payment_status = "pending"
    subscription.status = "payment_pending"
    subscription.amount_due = _to_decimal_amount(amount_kobo, kobo=True)
    subscription.warning_phase = None
    _log_subscription_event(
        db,
        subscription_id=subscription.id,
        event_type="subscription.payment_initialized",
        old_status=None,
        new_status=subscription.status,
        metadata={"reference": reference, "billingCycle": billing_cycle, "callbackUrl": callback_url},
    )
    return invoice, attempt


def _mark_payment_failure(
    db: Session,
    *,
    reference: str | None,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> None:
    invoice = _find_invoice_by_reference(db, reference)
    attempt = (
        db.query(PaymentAttempt)
        .filter(PaymentAttempt.provider == "paystack", PaymentAttempt.provider_reference == str(reference or ""))
        .order_by(PaymentAttempt.attempted_at.desc())
        .first()
    )
    if invoice:
        invoice.status = "failed"
        invoice.raw_payload = _serialize_json(payload)
    if attempt:
        attempt.status = "failed"
        attempt.failure_reason = reason
    if invoice:
        _log_subscription_event(
            db,
            subscription_id=invoice.subscription_id,
            event_type="subscription.payment_failed",
            metadata={"reference": reference, "reason": reason},
        )
    db.commit()


def _finalize_successful_payment(
    db: Session,
    *,
    user_id: str,
    plan_id: str,
    billing_cycle: str,
    reference: str,
    amount_kobo: int,
    currency: str,
    payload: dict[str, Any],
    source: str,
) -> tuple[Subscription, dict[str, Any]]:
    invoice = _find_invoice_by_reference(db, reference)
    if invoice and invoice.status == "paid":
        existing_subscription = (
            db.query(Subscription)
            .filter(Subscription.id == invoice.subscription_id)
            .first()
        )
        if existing_subscription:
            plan = get_plan_or_raise(db, plan_id)
            return existing_subscription, plan

    previous_subscription = get_user_subscription(db, user_id)
    previous_status = previous_subscription.status if previous_subscription else None
    row = activate_subscription(db, user_id=user_id, plan=plan_id, billing_cycle=billing_cycle, payment_status="paid")

    if invoice:
        invoice.subscription_id = row.id
        invoice.amount_received = _to_decimal_amount(amount_kobo, kobo=True)
        invoice.currency = currency
        invoice.status = "paid"
        invoice.paid_at = utc_now()
        invoice.raw_payload = _serialize_json(payload)

    attempt = (
        db.query(PaymentAttempt)
        .filter(PaymentAttempt.provider == "paystack", PaymentAttempt.provider_reference == reference)
        .order_by(PaymentAttempt.attempted_at.desc())
        .first()
    )
    if attempt:
        attempt.subscription_id = row.id
        attempt.invoice_id = invoice.id if invoice else attempt.invoice_id
        attempt.status = "confirmed"
        attempt.confirmed_at = utc_now()
        attempt.failure_code = None
        attempt.failure_reason = None

    row.amount_paid = _to_decimal_amount(amount_kobo, kobo=True)
    row.amount_due = Decimal("0")
    row.last_successful_payment_at = utc_now()
    _log_subscription_event(
        db,
        subscription_id=row.id,
        event_type="subscription.reactivated" if previous_status in {"grace_period", "suspended"} else "subscription.activated",
        old_status=previous_status,
        new_status=row.status,
        metadata={"reference": reference, "source": source, "planId": plan_id},
    )
    db.commit()
    db.refresh(row)
    plan = get_plan_or_raise(db, plan_id)
    return row, plan


def _create_default_estate_trial(db: Session, user_id: str) -> Subscription:
    now = utc_now()
    row = Subscription(
        user_id=user_id,
        plan="estate_starter",
        status="active",
        payment_status="trialing",
        billing_cycle="monthly",
        tenant_type="estate",
        tenant_id=user_id,
        billing_scope="estate",
        grace_days=DEFAULT_GRACE_DAYS,
        grace_ends_at=now + timedelta(days=30 + DEFAULT_GRACE_DAYS),
        starts_at=now,
        ends_at=now + timedelta(days=30),
        trial_started_at=now,
        trial_ends_at=now + timedelta(days=30),
        timezone="Africa/Lagos",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def activate_subscription(
    db: Session,
    user_id: str,
    plan: str,
    billing_cycle: str = "monthly",
    payment_status: str | None = None,
):
    plan_meta = get_plan_or_raise(db, plan, include_inactive=True)
    user = db.query(User).filter(User.id == user_id).first()
    if user and plan_meta.get("audience") not in {"legacy", user.role.value}:
        raise AppException("Selected plan is not available for this account type.", status_code=400)
    if plan_meta.get("manualActivationRequired"):
        raise AppException("This plan requires manual activation by an administrator.", status_code=400)

    now = utc_now()
    tenant_type, tenant_id, billing_scope = _resolve_subscription_scope(user, str(plan_meta.get("audience") or "homeowner"))
    replaced_statuses: list[str] = []
    for active_row in db.query(Subscription).filter(Subscription.user_id == user_id, Subscription.status == "active").all():
        replaced_statuses.append(active_row.status)
        active_row.status = "replaced"
        active_row.ends_at = active_row.ends_at or now

    duration_days = _resolve_duration_days(plan_meta, billing_cycle)
    ends_at = now + timedelta(days=duration_days) if duration_days else None
    trialing = int(plan_meta.get("trialDays") or 0) > 0 and int(plan_meta.get("amount") or 0) == 0
    payment_state = payment_status or ("trialing" if trialing else ("active" if int(plan_meta.get("amount") or 0) > 0 else "free"))
    row = Subscription(
        user_id=user_id,
        plan=plan,
        status="active",
        payment_status=payment_state,
        billing_cycle=(billing_cycle or "monthly").strip().lower() or "monthly",
        tenant_type=tenant_type,
        tenant_id=tenant_id,
        billing_scope=billing_scope,
        auto_renew=True,
        cancel_at_period_end=False,
        grace_days=DEFAULT_GRACE_DAYS,
        grace_ends_at=ends_at + timedelta(days=DEFAULT_GRACE_DAYS) if ends_at else None,
        last_payment_attempt_at=now if payment_state in {"pending", "paid", "active"} else None,
        last_successful_payment_at=now if payment_state in {"paid", "active", "free", "trialing"} else None,
        amount_due=0,
        amount_paid=plan_meta.get("amount") if payment_state in {"paid", "active"} else 0,
        timezone="Africa/Lagos",
        starts_at=now,
        ends_at=ends_at,
        trial_started_at=now if trialing else None,
        trial_ends_at=ends_at if trialing else None,
    )
    db.add(row)
    db.flush()
    _log_subscription_event(
        db,
        subscription_id=row.id,
        event_type="subscription.activated",
        old_status=replaced_statuses[-1] if replaced_statuses else None,
        new_status=row.status,
        metadata={"planId": plan, "billingCycle": row.billing_cycle, "paymentStatus": payment_state},
    )
    _award_referral_reward_if_eligible(db=db, subscribed_user_id=user_id, plan_meta=plan_meta)
    db.commit()
    db.refresh(row)
    return row


def _award_referral_reward_if_eligible(db: Session, subscribed_user_id: str, plan_meta: dict) -> None:
    if int(plan_meta.get("amount") or 0) <= 0:
        return

    user = db.query(User).filter(User.id == subscribed_user_id).first()
    if not user or not user.referred_by_user_id:
        return

    already_rewarded = (
        db.query(ReferralReward)
        .filter(ReferralReward.referred_user_id == subscribed_user_id)
        .first()
    )
    if already_rewarded:
        return

    referrer = db.query(User).filter(User.id == user.referred_by_user_id).first()
    if not referrer:
        return

    reward = ReferralReward(
        referrer_user_id=referrer.id,
        referred_user_id=user.id,
        plan_id=str(plan_meta.get("id") or ""),
        reward_amount=REFERRAL_REWARD_AMOUNT,
        currency=(plan_meta.get("currency") or "NGN").upper(),
    )
    db.add(reward)
    referrer.referral_earnings = int(referrer.referral_earnings or 0) + REFERRAL_REWARD_AMOUNT
    db.add(
        Notification(
            user_id=referrer.id,
            kind="referral.reward",
            payload=json.dumps(
                {
                    "message": f"You earned {reward.currency} {REFERRAL_REWARD_AMOUNT:,} referral reward.",
                    "referredUserId": user.id,
                    "plan": plan_meta.get("id"),
                    "amount": REFERRAL_REWARD_AMOUNT,
                    "currency": reward.currency,
                }
            ),
        )
    )


def list_payment_purposes(db: Session):
    return db.query(PaymentPurpose).order_by(PaymentPurpose.created_at.desc()).all()


def get_referral_summary(db: Session, user_id: str) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise AppException("User not found", status_code=404)

    total_referrals = db.query(User).filter(User.referred_by_user_id == user_id).count()
    rewarded_referrals = db.query(ReferralReward).filter(ReferralReward.referrer_user_id == user_id).count()
    recent_rewards = (
        db.query(ReferralReward)
        .filter(ReferralReward.referrer_user_id == user_id)
        .order_by(ReferralReward.created_at.desc())
        .limit(10)
        .all()
    )
    return {
        "referralCode": user.referral_code,
        "earnings": int(user.referral_earnings or 0),
        "rewardPerReferral": REFERRAL_REWARD_AMOUNT,
        "currency": "NGN",
        "totalReferrals": total_referrals,
        "rewardedReferrals": rewarded_referrals,
        "recentRewards": [
            {
                "referredUserId": row.referred_user_id,
                "plan": row.plan_id,
                "amount": int(row.reward_amount or 0),
                "currency": row.currency or "NGN",
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in recent_rewards
        ],
    }


def _ensure_default_plans(db: Session) -> None:
    existing = {row.id: row for row in db.query(SubscriptionPlan).all()}
    changed = False
    for row in DEFAULT_PLAN_CATALOG:
        plan = existing.get(row["id"])
        if not plan:
            plan = SubscriptionPlan(id=row["id"])
            db.add(plan)
        plan.name = row["name"]
        plan.amount = int(row["amount"])
        plan.currency = (row.get("currency") or "NGN").upper()
        plan.audience = row.get("audience", "homeowner")
        plan.max_doors = int(row.get("maxDoors") or 1)
        plan.max_qr_codes = int(row.get("maxQrCodes") or 1)
        plan.max_admins = int(row.get("maxAdmins") or 1)
        plan.duration_days = row.get("durationDays")
        plan.trial_days = int(row.get("trialDays") or 0)
        plan.self_serve = bool(row.get("selfServe", True))
        plan.manual_activation_required = bool(row.get("manualActivationRequired", False))
        plan.hidden = bool(row.get("hidden", False))
        plan.enabled_features = _encode_json_list(list(row.get("enabledFeatures") or []))
        plan.restrictions = _encode_json_list(list(row.get("restrictions") or []))
        plan.active = bool(row.get("active", True))
        changed = True
    if changed:
        db.commit()


def list_subscription_plans(db: Session, include_inactive: bool = False):
    _ensure_default_plans(db)
    q = db.query(SubscriptionPlan).order_by(SubscriptionPlan.amount.asc(), SubscriptionPlan.id.asc())
    if not include_inactive:
        q = q.filter(SubscriptionPlan.active == True)  # noqa: E712
    rows = q.all()
    return [_plan_payload(row, _catalog_row_by_id(row.id)) for row in rows]


def get_plan_or_raise(db: Session, plan_id: str, include_inactive: bool = False, user: User | None = None, *, now: datetime | None = None):
    _ensure_default_plans(db)
    q = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id)
    if not include_inactive:
        q = q.filter(SubscriptionPlan.active == True)  # noqa: E712
    row = q.first()
    if row:
        return _plan_payload(row, _catalog_row_by_id(row.id), user=user, now=now)
    raise AppException("Invalid plan selected", status_code=400)


def upsert_plan(
    db: Session,
    plan_id: str,
    name: str,
    amount: int,
    currency: str,
    max_doors: int,
    max_qr_codes: int,
    active: bool,
):
    _ensure_default_plans(db)
    row = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
    if not row:
        row = SubscriptionPlan(id=plan_id, name=name)
        db.add(row)
    row.name = name
    row.amount = int(amount)
    row.currency = currency or "NGN"
    row.max_doors = int(max_doors)
    row.max_qr_codes = int(max_qr_codes)
    row.active = bool(active)
    db.commit()
    db.refresh(row)
    return row


def get_user_subscription(db: Session, user_id: str):
    return (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id)
        .order_by(Subscription.starts_at.desc(), Subscription.id.desc())
        .first()
    )


def get_effective_subscription(db: Session, user_id: str, user_role: str | None = None):
    try:
        user = db.query(User).filter(User.id == user_id).first()
    except Exception:
        user = None
    audience = (user_role or (user.role.value if user else "") or "homeowner").strip().lower()
    managed_by_estate = False
    estate_id = None
    estate_name = None
    subscription_owner_id = user_id

    if audience == "homeowner":
        try:
            estate_row = (
                db.query(Home, Estate)
                .join(Estate, Estate.id == Home.estate_id)
                .filter(Home.homeowner_id == user_id, Home.estate_id.is_not(None))
                .order_by(Home.created_at.desc())
                .first()
            )
        except Exception:
            estate_row = None
        if estate_row:
            managed_by_estate = True
            estate_id = estate_row[1].id
            estate_name = estate_row[1].name
            subscription_owner_id = estate_row[1].owner_id or user_id
            audience = "estate"
    try:
        row = get_user_subscription(db, subscription_owner_id)
    except Exception:
        row = None

    if not row and audience == "estate":
        prior_trial = (
            db.query(Subscription)
            .filter(Subscription.user_id == user_id, Subscription.plan == "estate_starter")
            .order_by(Subscription.starts_at.desc(), Subscription.id.desc())
            .first()
        )
        row = prior_trial or _create_default_estate_trial(db, user_id)

    if not row:
        # Avoid depending on subscription-plan tables for the free baseline.
        free_plan = _catalog_row_by_id(_default_plan_id_for_audience(audience)) or {"id": "free", "name": "Free", "audience": audience}
        now = utc_now()
        free_feature_flags = _build_feature_flags(list(free_plan.get("enabledFeatures") or []), user=user, now=now)
        summary = {
            "status": "active",
            "days_to_expiry": None,
            "grace_days_left": 0,
            "is_bill_payer": False if managed_by_estate else audience in {"homeowner", "estate"},
            "warning_phase": None,
            "allowed_actions": {
                "view_dashboard": True,
                "renew_subscription": True,
                "view_logs": True,
                "view_messages": True,
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
                "manage_billing": audience in {"homeowner", "estate"},
            },
            "current_period_end": None,
            "grace_ends_at": None,
            "billing_scope": "estate" if audience == "estate" else "homeowner",
        }
        result = {
            "id": None,
            "plan": free_plan["id"],
            "planName": free_plan["name"],
            "status": summary["status"],
            "paymentStatus": "free",
            "audience": free_plan.get("audience", audience),
            "startsAt": None,
            "endsAt": None,
            "expiresAt": None,
            "isTrial": False,
            "trialStatus": "not_applicable",
            "trialDaysRemaining": 0,
            "expiresSoon": False,
            "requiresManualActivation": bool(free_plan.get("manualActivationRequired")),
            "limits": {
                "maxEstates": int(free_plan.get("maxEstates") or (1 if audience == "estate" else 0)),
                "maxHomes": int(free_plan.get("maxHomes") or free_plan.get("maxDoors") or 0),
                "maxDoors": int(free_plan.get("maxDoors") or 0),
                "maxQrCodes": int(free_plan.get("maxQrCodes") or 0),
                "maxAdmins": int(free_plan.get("maxAdmins") or 1),
                "logRetentionDays": LIMITED_LOG_RETENTION_DAYS if free_feature_flags.get("limited_logs") else 0,
            },
            "features": list(free_plan.get("enabledFeatures") or []),
            "featureFlags": free_feature_flags,
            "restrictions": list(free_plan.get("restrictions") or []),
            "billingCycle": "monthly",
        }
        result = _merge_subscription_summary(result, summary)
        result["managedByEstate"] = managed_by_estate
        result["subscriptionOwnerId"] = subscription_owner_id
        result["estateId"] = estate_id
        result["estateName"] = estate_name
        result["inSignupTrial"] = _is_user_in_signup_trial(user, now=now)
        return result

    try:
        now = utc_now()
        plan_meta = get_plan_or_raise(db, row.plan, include_inactive=True, user=user, now=now)
    except AppException:
        now = utc_now()
        plan_meta = get_plan_or_raise(db, _default_plan_id_for_audience(audience), user=user, now=now)
        row.plan = plan_meta["id"]
        db.commit()
    _apply_subscription_lifecycle(row, now=now)
    db.commit()
    db.refresh(row)
    expires_at = ensure_utc(row.ends_at or row.trial_ends_at)
    trial_days_remaining = 0
    if expires_at:
        trial_days_remaining = max((expires_at - now).days, 0)

    summary = build_subscription_summary(
        row,
        actor_role=audience,
        is_bill_payer=False if managed_by_estate else audience in {"homeowner", "estate"},
        now=now,
    )

    result = {
        "id": row.id,
        "plan": plan_meta["id"],
        "planName": plan_meta["name"],
        "status": summary["status"],
        "paymentStatus": row.payment_status or ("trialing" if plan_meta.get("trialDays") else "active"),
        "audience": plan_meta["audience"],
        "startsAt": row.starts_at.isoformat() if row.starts_at else None,
        "endsAt": row.ends_at.isoformat() if row.ends_at else None,
        "expiresAt": expires_at.isoformat() if expires_at else None,
        "isTrial": bool(plan_meta.get("trialDays") and int(plan_meta.get("amount") or 0) == 0),
        "trialStatus": "expired" if row.status == "expired" and plan_meta.get("trialDays") else ("active" if plan_meta.get("trialDays") else "not_applicable"),
        "trialDaysRemaining": trial_days_remaining if plan_meta.get("trialDays") else 0,
        "expiresSoon": bool(expires_at and 0 <= (expires_at - now).days <= 3),
        "requiresManualActivation": bool(plan_meta.get("manualActivationRequired")),
        "limits": {
            "maxEstates": int(plan_meta.get("maxEstates") or (0 if plan_meta["id"] == "estate_enterprise" else (1 if audience == "estate" else 0))),
            "maxHomes": int(plan_meta.get("maxHomes") or plan_meta["maxDoors"]),
            "maxDoors": plan_meta["maxDoors"],
            "maxQrCodes": plan_meta["maxQrCodes"],
            "maxAdmins": plan_meta["maxAdmins"],
            "logRetentionDays": LIMITED_LOG_RETENTION_DAYS if plan_meta["featureFlags"].get("limited_logs") else 0,
        },
        "features": plan_meta["enabledFeatures"],
        "featureFlags": plan_meta["featureFlags"],
        "restrictions": plan_meta["restrictions"],
        "billingCycle": row.billing_cycle or "monthly",
    }
    result = _merge_subscription_summary(result, summary)
    result["managedByEstate"] = managed_by_estate
    result["subscriptionOwnerId"] = subscription_owner_id
    result["estateId"] = estate_id
    result["estateName"] = estate_name
    result["inSignupTrial"] = _is_user_in_signup_trial(user, now=now)
    _notify_trial_and_expiry_windows(db, user_id=user_id, subscription=result)
    return result


def is_paid_subscription_expired(db: Session, user_id: str) -> bool:
    subscription = get_effective_subscription(db, user_id)
    if subscription.get("plan") in {"free", "estate_starter"} and subscription.get("status") in {"active", "expiring_soon", "trial"}:
        return False
    return subscription.get("status") == "suspended"


def require_subscription_feature(db: Session, user_id: str, feature: str, user_role: str | None = None) -> dict[str, Any]:
    subscription = get_effective_subscription(db, user_id, user_role=user_role)
    if subscription.get("status") == "suspended":
        raise AppException(
            "Your subscription has been paused. Renew now to restore visitor operations.",
            status_code=403,
            code="SUBSCRIPTION_ACTION_BLOCKED",
            extra={"subscription": subscription, "renew_url": "/billing/paywall"},
        )
    feature_key = str(feature or "").strip()
    if subscription.get("allowed_actions") and subscription.get("allowed_actions", {}).get(feature_key) is False:
        raise AppException(
            "This action is temporarily limited by your current subscription state.",
            status_code=403,
            code="SUBSCRIPTION_ACTION_BLOCKED",
            extra={"subscription": subscription, "renew_url": "/billing/paywall"},
        )
    if subscription.get("featureFlags", {}).get(feature_key):
        return subscription
    feature_name = FEATURE_LABELS.get(feature_key, feature_key.replace("_", " "))
    _insert_notification_if_missing(
        db,
        user_id=user_id,
        kind="subscription.feature.blocked",
        unique_key=f"feature-block:{feature_key}:{subscription.get('plan')}",
        message=f"{feature_name.title()} is not available on your current plan.",
        payload={"feature": feature_key, "plan": subscription.get("plan")},
    )
    raise AppException(
        f"{feature_name.title()} is not available on your {subscription.get('planName') or 'current'} plan. Upgrade to continue.",
        status_code=402,
    )


def initialize_paystack_transaction(
    user_id: str,
    email: str,
    plan_id: str,
    callback_url: str | None,
    billing_cycle: str = "monthly",
):
    raise AppException("Internal error: use initialize_paystack_transaction_db", status_code=500)


def initialize_paystack_transaction_db(
    db: Session,
    user_id: str,
    email: str,
    plan_id: str,
    callback_url: str | None,
    billing_cycle: str = "monthly",
):
    plan = get_plan_or_raise(db, plan_id)
    if not plan.get("selfServe", True):
        raise AppException("This plan requires manual sales onboarding", status_code=400)
    if plan["amount"] <= 0:
        raise AppException("Free plan does not require Paystack checkout", status_code=400)
    cycle = (billing_cycle or "monthly").strip().lower()
    if cycle not in {"monthly", "yearly"}:
        raise AppException("Invalid billing cycle", status_code=400)
    paystack_secret = _normalize_secret(settings.PAYSTACK_SECRET_KEY)
    if not paystack_secret:
        raise AppException("Paystack is not configured", status_code=500)
    frontend_base_url = _normalize_url(settings.FRONTEND_BASE_URL)
    if paystack_secret.startswith("sk_live") and (
        "localhost" in frontend_base_url or "127.0.0.1" in frontend_base_url
    ):
        raise AppException(
            "Live Paystack cannot be initialized with localhost frontend. Use a public HTTPS domain in FRONTEND_BASE_URL or use test keys for local development.",
            status_code=400,
        )

    reference = f"qring-{uuid.uuid4().hex[:18]}"
    subscription_record = _ensure_subscription_billing_record(
        db,
        user_id=user_id,
        plan_id=plan_id,
        billing_cycle=cycle,
    )
    payload = {
        "email": email,
        "amount": _compute_expected_amount_kobo(plan["amount"], cycle),
        "currency": (plan.get("currency") or "NGN").upper(),
        "reference": reference,
        "metadata": {
            "user_id": user_id,
            "plan": plan_id,
            "billing_cycle": cycle,
            "source": "qring-billing",
        },
    }
    _create_or_update_invoice_and_attempt(
        db,
        subscription=subscription_record,
        reference=reference,
        amount_kobo=int(payload["amount"]),
        currency=(plan.get("currency") or "NGN").upper(),
        billing_cycle=cycle,
        callback_url=callback_url,
        payload=payload,
    )
    db.commit()

    normalized_callback = _normalize_url(callback_url)
    resolved_callback = normalized_callback or f"{frontend_base_url}/billing/callback"
    callback_is_public_https = _is_public_https_url(resolved_callback)
    if paystack_secret.startswith("sk_live") and not callback_is_public_https:
        raise AppException(
            "Live Paystack requires a public HTTPS callback URL. Set callbackUrl from frontend or FRONTEND_BASE_URL to your production HTTPS domain.",
            status_code=400,
        )
    if callback_is_public_https:
        payload["callback_url"] = resolved_callback
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        "https://api.paystack.co/transaction/initialize",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {paystack_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "QringBackend/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        error_code, error_message = _extract_paystack_error(detail)
        _mark_payment_failure(
            db,
            reference=reference,
            reason=f"paystack initialize failed: {error_message or detail}",
            payload={"detail": detail, "code": error_code},
        )
        if error_code == "1010":
            raise AppException(
                f"Paystack blocked initialization (1010: {error_message or 'operation blocked'}). "
                f"Check Paystack live-mode restrictions: callback/domain allowlist and server IP allowlist. "
                f"frontendBaseUrl={frontend_base_url}, callback={resolved_callback if callback_is_public_https else '<omitted>'}",
                status_code=502,
            )
        raise AppException(f"Paystack initialize failed: {error_message or detail}", status_code=502)
    except error.URLError as exc:
        reason = getattr(exc, "reason", None)
        _mark_payment_failure(
            db,
            reference=reference,
            reason=f"paystack initialize network error ({reason or 'unreachable'})",
            payload={"reason": str(reason or "unreachable")},
        )
        raise AppException(
            f"Paystack initialize failed: upstream network error ({reason or 'unreachable'}).",
            status_code=502,
        )
    except Exception:
        _mark_payment_failure(db, reference=reference, reason="paystack initialize failed", payload={})
        raise AppException("Paystack initialize failed", status_code=502)

    if not data.get("status") or not data.get("data", {}).get("authorization_url"):
        _mark_payment_failure(db, reference=reference, reason="paystack initialize returned no authorization url", payload=data)
        raise AppException("Unable to initialize payment", status_code=502)
    return data["data"]


def verify_paystack_and_activate(db: Session, reference: str, user_id: str):
    paystack_secret = _normalize_secret(settings.PAYSTACK_SECRET_KEY)
    if not paystack_secret:
        raise AppException("Paystack is not configured", status_code=500)

    req = request.Request(
        f"https://api.paystack.co/transaction/verify/{reference}",
        method="GET",
        headers={
            "Authorization": f"Bearer {paystack_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "QringBackend/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        _mark_payment_failure(db, reference=reference, reason=f"paystack verify failed: {detail}", payload={"detail": detail})
        raise AppException(f"Paystack verify failed: {detail}", status_code=502)
    except error.URLError as exc:
        reason = getattr(exc, "reason", None)
        _mark_payment_failure(
            db,
            reference=reference,
            reason=f"paystack verify network error ({reason or 'unreachable'})",
            payload={"reason": str(reason or "unreachable")},
        )
        raise AppException(
            f"Paystack verify failed: upstream network error ({reason or 'unreachable'}).",
            status_code=502,
        )
    except Exception:
        _mark_payment_failure(db, reference=reference, reason="paystack verify failed", payload={})
        raise AppException("Paystack verify failed", status_code=502)

    if not data.get("status"):
        _mark_payment_failure(db, reference=reference, reason="unable to verify payment", payload=data)
        raise AppException("Unable to verify payment", status_code=400)

    payment = data.get("data", {})
    if payment.get("status") != "success":
        _mark_payment_failure(
            db,
            reference=reference,
            reason=f"payment not successful ({payment.get('status') or 'unknown'})",
            payload=payment,
        )
        raise AppException("Payment not successful", status_code=400)

    metadata = payment.get("metadata") or {}
    payment_user_id = metadata.get("user_id")
    plan_id = metadata.get("plan")
    billing_cycle = metadata.get("billing_cycle") or "monthly"
    if payment_user_id != user_id:
        raise AppException("Payment reference is not linked to this user", status_code=403)
    plan = get_plan_or_raise(db, plan_id)
    expected_amount_kobo = _compute_expected_amount_kobo(plan["amount"], billing_cycle)
    paid_amount_kobo = int(payment.get("amount") or 0)
    paid_currency = str(payment.get("currency") or "").upper()
    expected_currency = (plan.get("currency") or "NGN").upper()
    if paid_amount_kobo != expected_amount_kobo or paid_currency != expected_currency:
        _mark_payment_failure(
            db,
            reference=reference,
            reason="payment amount or currency mismatch",
            payload=payment,
        )
        raise AppException(
            "Payment amount or currency does not match selected plan",
            status_code=400,
        )

    row, plan = _finalize_successful_payment(
        db,
        user_id=user_id,
        plan_id=plan["id"],
        billing_cycle=billing_cycle,
        reference=reference,
        amount_kobo=paid_amount_kobo,
        currency=paid_currency,
        payload=payment,
        source="paystack_verify",
    )
    try:
        from app.services.advanced_service import create_digital_receipt

        create_digital_receipt(
            db,
            owner_user_id=user_id,
            reference=reference,
            amount_kobo=paid_amount_kobo,
            currency=paid_currency,
            purpose="subscription",
            payload={
                "planId": plan["id"],
                "billingCycle": billing_cycle,
                "source": "paystack_verify",
            },
        )
    except Exception:
        # Keep subscription activation resilient even if receipt persistence fails.
        pass
    return {
        "id": row.id,
        "plan": row.plan,
        "status": row.status,
        "startsAt": row.starts_at.isoformat() if row.starts_at else None,
        "endsAt": row.ends_at.isoformat() if row.ends_at else None,
        "limits": {
            "maxDoors": plan["maxDoors"],
            "maxQrCodes": plan["maxQrCodes"],
        },
    }


def handle_paystack_webhook(db: Session, raw_body: bytes, signature: str | None):
    paystack_secret = _normalize_secret(settings.PAYSTACK_SECRET_KEY)
    if not paystack_secret:
        raise AppException("Paystack is not configured", status_code=500)
    if not signature:
        raise AppException("Missing Paystack signature", status_code=400)

    computed = hmac.new(
        paystack_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=sha512,
    ).hexdigest()

    if not hmac.compare_digest(computed, signature):
        raise AppException("Invalid Paystack signature", status_code=401)

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise AppException("Invalid webhook payload", status_code=400)

    event_name = str(event.get("event") or "").strip().lower()
    if event_name not in {"charge.success", "charge.failed"}:
        return {"status": "ignored"}

    data = event.get("data") or {}
    metadata = data.get("metadata") or {}
    if metadata.get("payment_kind") == "estate_alert":
        from app.services.estate_alert_service import apply_alert_payment_webhook

        return apply_alert_payment_webhook(
            db=db,
            metadata=metadata,
            reference=data.get("reference"),
            status=data.get("status") or ("failed" if event_name == "charge.failed" else "success"),
            amount_kobo=data.get("amount"),
            paid_at_iso=data.get("paid_at"),
            paystack_transaction_id=data.get("id"),
        )

    user_id = metadata.get("user_id")
    plan_id = metadata.get("plan")
    payment_status = data.get("status")
    reference = str(data.get("reference") or "")
    if event_name == "charge.failed" or payment_status != "success":
        _mark_payment_failure(
            db,
            reference=reference,
            reason=f"webhook reported {payment_status or event_name}",
            payload=data,
        )
        return {"status": "ignored"}
    if not user_id or not plan_id:
        return {"status": "ignored"}

    row, _ = _finalize_successful_payment(
        db=db,
        user_id=user_id,
        plan_id=plan_id,
        billing_cycle=str(metadata.get("billing_cycle") or "monthly"),
        reference=reference or f"webhook-{uuid.uuid4().hex[:10]}",
        amount_kobo=int(data.get("amount") or 0),
        currency=str(data.get("currency") or "NGN").upper(),
        payload=data,
        source="paystack_webhook",
    )
    try:
        from app.services.advanced_service import create_digital_receipt

        create_digital_receipt(
            db,
            owner_user_id=user_id,
            reference=reference or f"webhook-{uuid.uuid4().hex[:10]}",
            amount_kobo=int(data.get("amount") or 0),
            currency=str(data.get("currency") or "NGN").upper(),
            purpose="subscription",
            payload={"planId": plan_id, "source": "paystack_webhook"},
        )
    except Exception:
        pass
    return {"status": "processed", "plan": plan_id, "subscriptionId": row.id}
