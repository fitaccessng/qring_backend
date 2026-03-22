from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.time import ensure_utc, utc_now
from app.db.models import Notification, Subscription, SubscriptionNotification
from app.services.subscription_policy_service import (
    compute_warning_phase,
    create_subscription_event,
    should_send_warning,
    sync_subscription_lifecycle,
)


WARNING_TEMPLATE_KEY = "subscription.expiry_warning"
GRACE_TEMPLATE_KEY = "subscription.grace_started"
SUSPENSION_TEMPLATE_KEY = "subscription.suspended"


def _days_to_expiry(subscription: Subscription, *, now: datetime) -> int | None:
    expires_at = ensure_utc(subscription.ends_at or subscription.trial_ends_at)
    if not expires_at:
        return None
    delta = expires_at - now
    return max(0, int((delta.total_seconds() + 86399) // 86400))


def _notification_exists(db: Session, *, dedupe_key: str) -> bool:
    return (
        db.query(SubscriptionNotification.id)
        .filter(SubscriptionNotification.dedupe_key == dedupe_key)
        .first()
        is not None
    )


def _insert_delivery_records(
    db: Session,
    *,
    subscription: Subscription,
    template_key: str,
    dedupe_key: str,
    notification_kind: str,
    message: str,
    warning_phase: str | None,
    now: datetime,
    extra_payload: dict[str, Any] | None = None,
) -> bool:
    if _notification_exists(db, dedupe_key=dedupe_key):
        return False

    db.add(
        SubscriptionNotification(
            subscription_id=subscription.id,
            channel="in_app",
            template_key=template_key,
            warning_phase=warning_phase,
            scheduled_for=now,
            sent_at=now,
            delivery_status="sent",
            dedupe_key=dedupe_key,
        )
    )
    payload = {
        "message": message,
        "subscriptionId": subscription.id,
        "templateKey": template_key,
        "status": subscription.status,
        "warningPhase": warning_phase,
        "currentPeriodEnd": subscription.ends_at.isoformat() if subscription.ends_at else None,
        "graceEndsAt": subscription.grace_ends_at.isoformat() if subscription.grace_ends_at else None,
    }
    if extra_payload:
        payload.update(extra_payload)
    db.add(
        Notification(
            user_id=subscription.user_id,
            kind=notification_kind,
            payload=json.dumps(payload),
        )
    )
    return True


def _queue_warning_notification(db: Session, *, subscription: Subscription, now: datetime) -> bool:
    days_to_expiry = _days_to_expiry(subscription, now=now)
    if days_to_expiry is None:
        return False
    warning_phase = compute_warning_phase(subscription, now=now)
    period_key = subscription.ends_at.isoformat() if subscription.ends_at else "none"
    dedupe_key = f"{subscription.id}:warning:{period_key}:{days_to_expiry}"
    message = (
        f"Your {subscription.plan} plan expires in {days_to_expiry} day(s). "
        "Renew now to avoid service interruption."
    )
    created = _insert_delivery_records(
        db,
        subscription=subscription,
        template_key=WARNING_TEMPLATE_KEY,
        dedupe_key=dedupe_key,
        notification_kind="subscription.warning",
        message=message,
        warning_phase=warning_phase,
        now=now,
        extra_payload={"daysToExpiry": days_to_expiry},
    )
    if created:
        db.add(
            create_subscription_event(
                subscription_id=subscription.id,
                event_type="subscription.warning_sent",
                old_status=subscription.status,
                new_status=subscription.status,
                metadata={
                    "daysToExpiry": days_to_expiry,
                    "warningPhase": warning_phase,
                    "scheduledFor": now.isoformat(),
                },
            )
        )
    return created


def _queue_transition_notification(
    db: Session,
    *,
    subscription: Subscription,
    previous_status: str | None,
    now: datetime,
) -> bool:
    if subscription.status == "grace_period":
        template_key = GRACE_TEMPLATE_KEY
        notification_kind = "subscription.grace_started"
        message = (
            f"Your {subscription.plan} plan is now in grace period. "
            "Renew before the grace window ends to avoid suspension."
        )
        marker = subscription.grace_ends_at.isoformat() if subscription.grace_ends_at else now.isoformat()
    elif subscription.status == "suspended":
        template_key = SUSPENSION_TEMPLATE_KEY
        notification_kind = "subscription.suspended"
        message = (
            f"Your {subscription.plan} plan has been suspended for non-payment. "
            "Renew now to restore visitor operations."
        )
        marker = subscription.grace_ends_at.isoformat() if subscription.grace_ends_at else now.isoformat()
    else:
        return False

    return _insert_delivery_records(
        db,
        subscription=subscription,
        template_key=template_key,
        dedupe_key=f"{subscription.id}:status:{subscription.status}:{marker}",
        notification_kind=notification_kind,
        message=message,
        warning_phase=subscription.warning_phase,
        now=now,
        extra_payload={"previousStatus": previous_status},
    )


def run_subscription_lifecycle_jobs(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    current_time = ensure_utc(now) or utc_now()
    candidate_rows = (
        db.query(Subscription)
        .order_by(Subscription.user_id.asc(), Subscription.starts_at.desc(), Subscription.id.desc())
        .all()
    )
    rows: list[Subscription] = []
    seen_user_ids: set[str] = set()
    for row in candidate_rows:
        if row.user_id in seen_user_ids:
            continue
        seen_user_ids.add(row.user_id)
        rows.append(row)

    if not rows:
        return {
            "status": "ok",
            "checked": 0,
            "warnings_sent": 0,
            "entered_grace": 0,
            "suspended": 0,
        }

    summary = {
        "status": "ok",
        "checked": 0,
        "warnings_sent": 0,
        "entered_grace": 0,
        "suspended": 0,
    }

    for row in rows:
        summary["checked"] += 1
        lifecycle = sync_subscription_lifecycle(row, now=current_time)
        if lifecycle["status_changed"]:
            event_type = (
                "subscription.entered_grace"
                if row.status == "grace_period"
                else "subscription.suspended"
                if row.status == "suspended"
                else "subscription.expiring_soon"
            )
            db.add(
                create_subscription_event(
                    subscription_id=row.id,
                    event_type=event_type,
                    old_status=lifecycle["previous_status"],
                    new_status=row.status,
                    metadata={
                        "expiresAt": lifecycle["expires_at"].isoformat() if lifecycle["expires_at"] else None,
                        "graceEndsAt": lifecycle["grace_ends_at"].isoformat() if lifecycle["grace_ends_at"] else None,
                        "jobRunAt": current_time.isoformat(),
                    },
                )
            )
            if _queue_transition_notification(
                db,
                subscription=row,
                previous_status=lifecycle["previous_status"],
                now=current_time,
            ):
                if row.status == "grace_period":
                    summary["entered_grace"] += 1
                elif row.status == "suspended":
                    summary["suspended"] += 1

        if row.status in {"active", "expiring_soon", "trial"} and should_send_warning(row, now=current_time):
            if _queue_warning_notification(db, subscription=row, now=current_time):
                summary["warnings_sent"] += 1

    db.commit()
    return summary
