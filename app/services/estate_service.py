from __future__ import annotations

import uuid
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, aliased
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppException
from app.core.config import get_settings
from app.core.time import utc_now
from app.core.security import hash_password
from app.db.models import (
    Door,
    Estate,
    EstateAlert,
    EstateAlertType,
    EstatePackage,
    GateLog,
    GuardAttendance,
    Home,
    HomeownerPayment,
    Notification,
    QRCode,
    ResidentVehicle,
    SecurityIncident,
    User,
    UserRole,
    VisitorSession,
)
from app.services.payment_service import get_effective_subscription, is_paid_subscription_expired, require_subscription_feature
from app.services.provider_integrations import send_push_fcm, send_transactional_email
settings = get_settings()
FREE_ESTATE_LIMIT = 5


def _build_estate_invite_email_body(
    *,
    estate_name: str,
    resident_name: str,
    unit_name: str,
    email: str,
    temporary_password: str | None,
    login_link: str,
    invite_token: str,
) -> str:
    lines = [
        f"Hello {resident_name},",
        "",
        f"You have been added to {estate_name} on Qring as a resident.",
        "",
        "Your account details:",
        f"Resident Name: {resident_name}",
        f"Unit: {unit_name}",
        f"Email: {email}",
    ]
    if temporary_password:
        lines.append(f"Temporary Password: {temporary_password}")
    lines.extend(
        [
            "",
            f"Login URL: {login_link}",
            f"Invite Token: {invite_token}",
            "",
            "Use these details to sign in to your estate resident account.",
            "For security, please change your password after your first login.",
        ]
    )
    return "\n".join(lines)


def _generate_estate_join_code(db: Session) -> str:
    # Short, human-shareable token. Not meant to be secret-grade, just unguessable enough for casual entry.
    # Example: QR-EST-8F3K2D
    for _ in range(30):
        token = f"QR-EST-{uuid.uuid4().hex[:6].upper()}"
        exists = db.query(Estate).filter(Estate.join_code == token).first()
        if not exists:
            return token
    raise AppException("Unable to generate estate join code", status_code=500)


def _require_estate_owner(db: Session, estate_id: str, owner_id: str) -> Estate:
    estate = db.query(Estate).filter(Estate.id == estate_id, Estate.owner_id == owner_id).first()
    if not estate:
        raise AppException("Estate not found for this account", status_code=404)
    return estate


def _estate_scope_homes_query(db: Session, owner_id: str):
    return db.query(Home).join(Estate, Estate.id == Home.estate_id).filter(Estate.owner_id == owner_id)


def _usage_for_owner(db: Session, owner_id: str) -> dict[str, int]:
    home_ids = [row.id for row in _estate_scope_homes_query(db, owner_id).all()]
    if not home_ids:
        return {"homes": 0, "doors": 0, "qr_codes": 0}
    door_ids = [row.id for row in db.query(Door).filter(Door.home_id.in_(home_ids)).all()]
    qr_count = (
        db.query(func.count(func.distinct(QRCode.home_id)))
        .filter(
            QRCode.home_id.in_(home_ids),
            QRCode.active.is_(True),
            QRCode.mode != "selector",
        )
        .scalar()
        if home_ids
        else 0
    )
    return {
        "homes": len(home_ids),
        "doors": len(door_ids),
        "qr_codes": qr_count,
    }


def _house_qr_mode(home_id: str) -> str:
    return f"house:{home_id}"


def _ensure_house_qr(db: Session, *, estate_id: str, home: Home, door: Door) -> QRCode:
    existing = (
        db.query(QRCode)
        .filter(QRCode.home_id == home.id, QRCode.mode == _house_qr_mode(home.id), QRCode.active.is_(True))
        .order_by(QRCode.created_at.desc())
        .first()
    )
    if existing:
        if door.id not in [item.strip() for item in (existing.doors_csv or "").split(",") if item.strip()]:
            existing.doors_csv = ",".join([item for item in [existing.doors_csv, door.id] if item])
            db.flush()
        return existing
    qr = QRCode(
        qr_id=f"qr-{uuid.uuid4().hex[:12]}",
        plan="single",
        home_id=home.id,
        doors_csv=door.id,
        mode=_house_qr_mode(home.id),
        estate_id=estate_id,
        active=True,
    )
    db.add(qr)
    db.flush()
    return qr


def _estate_plan_capacity(subscription: dict[str, Any]) -> dict[str, int]:
    limits = (subscription or {}).get("limits") or {}
    max_estates = int(limits.get("maxEstates") or 0)
    max_homes = int(limits.get("maxHomes") or limits.get("maxDoors") or 0)
    max_doors = int(limits.get("maxDoors") or 0)
    max_qr_codes = int(limits.get("maxQrCodes") or 0)
    if (subscription or {}).get("plan") == "free":
        max_estates = max(max_estates, 1)
        max_homes = max(max_homes, FREE_ESTATE_LIMIT)
        max_doors = max(max_doors, FREE_ESTATE_LIMIT)
        max_qr_codes = max(max_qr_codes, FREE_ESTATE_LIMIT)
    return {
        "maxEstates": max_estates,
        "maxHomes": max_homes,
        "maxDoors": max_doors,
        "maxQrCodes": max_qr_codes,
    }


def _enforce_estate_limit(db: Session, owner_id: str, subscription: dict[str, Any]) -> None:
    max_estates = _estate_plan_capacity(subscription)["maxEstates"]
    if max_estates <= 0:
        return
    used_estates = db.query(Estate).filter(Estate.owner_id == owner_id).count()
    if used_estates >= max_estates:
        raise AppException(
            f"Your {subscription.get('planName') or subscription.get('plan') or 'current'} plan supports only {max_estates} estate"
            f"{'' if max_estates == 1 else 's'}. Upgrade to add another estate.",
            status_code=402,
        )


def _enforce_home_limit(db: Session, owner_id: str, subscription: dict[str, Any]) -> None:
    # Estate billing is house/unit based. Houses over the included capacity are billed
    # as add-ons, so adding a unit should not force an upgrade by itself.
    return


def _limited_log_cutoff(subscription: dict[str, Any]) -> datetime | None:
    retention_days = int(((subscription or {}).get("limits") or {}).get("logRetentionDays") or 0)
    if retention_days <= 0:
        return None
    return utc_now() - timedelta(days=retention_days)


def _notify_usage_threshold(
    db: Session,
    *,
    user_id: str,
    subscription: dict[str, Any],
    metric: str,
    used: int,
    limit: int,
) -> None:
    if limit <= 0:
        return
    if used < max(int(limit * 0.8), limit - 1):
        return
    unique_key = f"usage:{subscription.get('plan')}:{metric}:{used}:{limit}"
    recent = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.kind == "subscription.usage.warning")
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )
    for row in recent:
        try:
            payload = json.loads(row.payload or "{}")
        except Exception:
            payload = {}
        if str(payload.get("uniqueKey") or "") == unique_key:
            return
    db.add(
        Notification(
            user_id=user_id,
            kind="subscription.usage.warning",
            payload=json.dumps(
                {
                    "uniqueKey": unique_key,
                    "metric": metric,
                    "used": used,
                    "limit": limit,
                    "plan": subscription.get("plan"),
                    "message": f"You are close to your {metric.replace('_', ' ')} limit ({used}/{limit}). Upgrade soon to avoid interruption.",
                }
            ),
        )
    )
    db.commit()


def list_estate_overview(db: Session, owner_id: str) -> dict[str, Any]:
    if is_paid_subscription_expired(db, owner_id):
        estate_ids = [row.id for row in db.query(Estate).filter(Estate.owner_id == owner_id).all()]
        if estate_ids:
            db.query(QRCode).filter(QRCode.estate_id.in_(estate_ids), QRCode.active.is_(True)).update(
                {QRCode.active: False},
                synchronize_session=False,
            )
            db.commit()

    estates = db.query(Estate).filter(Estate.owner_id == owner_id).order_by(Estate.created_at.desc()).all()
    homes = _estate_scope_homes_query(db, owner_id).order_by(Home.created_at.desc()).all()
    home_ids = [home.id for home in homes]
    doors = db.query(Door).filter(Door.home_id.in_(home_ids)).order_by(Door.name.asc()).all() if home_ids else []

    homeowner_ids = sorted({home.homeowner_id for home in homes if home.homeowner_id})
    homeowners = (
        db.query(User).filter(User.id.in_(homeowner_ids)).all() if homeowner_ids else []
    )
    homeowner_by_id = {user.id: user for user in homeowners}
    home_by_id = {home.id: home for home in homes}
    estate_ids = [estate.id for estate in estates]
    security_users = (
        db.query(User)
        .filter(User.estate_id.in_(estate_ids), User.role == UserRole.security)
        .order_by(User.full_name.asc())
        .all()
        if estate_ids
        else []
    )

    qr_rows = db.query(QRCode).filter(QRCode.home_id.in_(home_ids), QRCode.active.is_(True)).all() if home_ids else []
    qr_by_door: dict[str, list[str]] = {}
    for qr in qr_rows:
        for door_id in [item.strip() for item in (qr.doors_csv or "").split(",") if item.strip()]:
            qr_by_door.setdefault(door_id, []).append(qr.qr_id)

    usage = _usage_for_owner(db, owner_id)
    effective_sub = get_effective_subscription(db, owner_id)
    capacity = _estate_plan_capacity(effective_sub)
    _notify_usage_threshold(
        db,
        user_id=owner_id,
        subscription=effective_sub,
        metric="doors",
        used=usage["doors"],
        limit=capacity["maxDoors"],
    )
    session_rows = (
        db.query(VisitorSession)
        .filter(VisitorSession.estate_id.in_(estate_ids))
        .order_by(VisitorSession.started_at.desc())
        .limit(400)
        .all()
        if estate_ids
        else []
    )
    gate_logs = (
        db.query(GateLog)
        .filter(GateLog.estate_id.in_(estate_ids))
        .order_by(GateLog.created_at.desc())
        .limit(60)
        .all()
        if estate_ids
        else []
    )
    hour_counts: dict[int, int] = {}
    home_visit_counts: dict[str, int] = {}
    approval_minutes: list[float] = []
    for row in session_rows:
        if row.started_at:
            hour_counts[row.started_at.hour] = hour_counts.get(row.started_at.hour, 0) + 1
        if row.home_id:
            home_visit_counts[row.home_id] = home_visit_counts.get(row.home_id, 0) + 1
        if row.started_at and row.homeowner_decision_at:
            approval_minutes.append(max(0.0, (row.homeowner_decision_at - row.started_at).total_seconds() / 60))
    peak_hours = [
        {"hour": hour, "count": count}
        for hour, count in sorted(hour_counts.items(), key=lambda item: item[1], reverse=True)[:5]
    ]
    most_visited_houses = [
        {
            "homeId": home_id,
            "homeName": home_by_id[home_id].name if home_id in home_by_id else "Home",
            "visits": count,
        }
        for home_id, count in sorted(home_visit_counts.items(), key=lambda item: item[1], reverse=True)[:5]
    ]
    avg_approval_time_minutes = round(sum(approval_minutes) / len(approval_minutes), 1) if approval_minutes else 0.0

    return {
        "estates": [
            {
                "id": row.id,
                "name": row.name,
                "status": "active",
                "createdAt": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
                "reminderFrequencyDays": int(row.reminder_frequency_days or 1),
            }
            for row in estates
        ],
        "homes": [
            {
                "id": row.id,
                "name": row.name,
                "estateId": row.estate_id,
                "homeownerId": row.homeowner_id,
                "homeownerName": homeowner_by_id[row.homeowner_id].full_name if row.homeowner_id in homeowner_by_id else "",
                "homeownerEmail": homeowner_by_id[row.homeowner_id].email if row.homeowner_id in homeowner_by_id else "",
                "homeownerRoleLabel": (
                    "Estate Homeowner"
                    if row.homeowner_id in homeowner_by_id and homeowner_by_id[row.homeowner_id].estate_id == row.estate_id
                    else "Homeowner"
                ),
            }
            for row in homes
        ],
        "doors": [
            {
                "id": row.id,
                "name": row.name,
                "homeId": row.home_id,
                "estateId": home_by_id[row.home_id].estate_id if row.home_id in home_by_id else "",
                "homeName": home_by_id[row.home_id].name if row.home_id in home_by_id else "",
                "homeownerId": home_by_id[row.home_id].homeowner_id if row.home_id in home_by_id else "",
                "homeownerName": (
                    homeowner_by_id[home_by_id[row.home_id].homeowner_id].full_name
                    if row.home_id in home_by_id and home_by_id[row.home_id].homeowner_id in homeowner_by_id
                    else ""
                ),
                "homeownerEmail": (
                    homeowner_by_id[home_by_id[row.home_id].homeowner_id].email
                    if row.home_id in home_by_id and home_by_id[row.home_id].homeowner_id in homeowner_by_id
                    else ""
                ),
                "loginLink": f"{settings.FRONTEND_BASE_URL.rstrip('/')}/login",
                "state": "Online" if row.is_active == "online" else "Offline",
                "qr": qr_by_door.get(row.id, []),
            }
            for row in doors
        ],
        "homeowners": [
            {
                "id": row.id,
                "fullName": row.full_name,
                "email": row.email,
                "active": row.is_active,
                "estateId": row.estate_id,
                "accountType": "estate_homeowner" if row.estate_id else "homeowner",
                "roleLabel": "Estate Homeowner" if row.estate_id else "Homeowner",
                "managedByEstate": bool(row.estate_id),
            }
            for row in homeowners
        ],
        "securityUsers": [
            {
                "id": row.id,
                "fullName": row.full_name,
                "email": row.email,
                "phone": row.phone,
                "gateId": row.gate_id,
                "estateId": row.estate_id,
                "active": row.is_active,
            }
            for row in security_users
        ],
        "securityRules": [
            {
                "estateId": row.id,
                "estateName": row.name,
                "canApproveWithoutHomeowner": bool(row.security_can_approve_without_homeowner),
                "mustNotifyHomeowner": bool(row.security_must_notify_homeowner),
                "requirePhotoVerification": bool(row.security_require_photo_verification),
                "requireCallBeforeApproval": bool(row.security_require_call_before_approval),
                "autoApproveTrustedVisitors": bool(row.auto_approve_trusted_visitors),
            }
            for row in estates
        ],
        "analytics": {
            "peakEntryTimes": peak_hours,
            "mostVisitedHouses": most_visited_houses,
            "averageApprovalTimeMinutes": avg_approval_time_minutes,
            "totalDailyVisitors": len([row for row in session_rows if row.started_at and row.started_at.date() == utc_now().date()]),
            "securityActivityLogs": [
                {
                    "id": row.id,
                    "action": row.action,
                    "gateId": row.gate_id,
                    "actorRole": row.actor_role,
                    "createdAt": row.created_at.isoformat() if row.created_at else None,
                    "resultingStatus": row.resulting_status,
                }
                for row in gate_logs
            ],
        },
        "planRestrictions": {
            "maxEstates": capacity["maxEstates"],
            "maxHomes": capacity["maxHomes"],
            "maxDoors": capacity["maxDoors"],
            "maxQrCodes": capacity["maxQrCodes"],
            "usedEstates": len(estates),
            "usedHomes": usage["homes"],
            "usedDoors": usage["doors"],
            "usedQrCodes": usage["qr_codes"],
            "remainingEstates": max(capacity["maxEstates"] - len(estates), 0) if capacity["maxEstates"] > 0 else 0,
            "remainingHomes": max(capacity["maxHomes"] - usage["homes"], 0) if capacity["maxHomes"] > 0 else 0,
            "remainingDoors": max(capacity["maxDoors"] - usage["doors"], 0),
            "remainingQrCodes": max(capacity["maxQrCodes"] - usage["qr_codes"], 0),
        },
        "subscription": effective_sub,
    }


def create_estate(db: Session, name: str, owner_id: str) -> Estate:
    estate_name = (name or "").strip()
    if not estate_name:
        raise AppException("Estate name is required", status_code=400)
    subscription = get_effective_subscription(db, owner_id, user_role="estate")
    _enforce_estate_limit(db, owner_id, subscription)
    estate = Estate(name=estate_name, owner_id=owner_id, join_code=_generate_estate_join_code(db))
    db.add(estate)
    db.commit()
    db.refresh(estate)
    return estate


def join_estate_by_token(
    db: Session,
    *,
    homeowner_id: str,
    join_token: str,
    unit_name: str,
) -> dict[str, Any]:
    token = (join_token or "").strip()
    if not token:
        raise AppException("Estate code or estate ID is required", status_code=400)

    clean_unit_name = (unit_name or "").strip()
    if not clean_unit_name:
        raise AppException("Unit / house label is required", status_code=400)

    estate = db.query(Estate).filter((Estate.join_code == token) | (Estate.id == token)).first()
    if not estate:
        raise AppException("Estate not found. Check the code/ID and try again.", status_code=404)

    existing = (
        db.query(Home)
        .filter(Home.homeowner_id == homeowner_id, Home.estate_id.is_not(None))
        .order_by(Home.created_at.desc())
        .first()
    )
    if existing:
        raise AppException("This account is already linked to an estate.", status_code=409)

    home = add_home(db=db, name=clean_unit_name, estate_id=estate.id, homeowner_id=homeowner_id, owner_id=None)
    return {
        "estateId": estate.id,
        "estateName": estate.name,
        "homeId": home.id,
        "homeName": home.name,
    }


def get_estate_settings(db: Session, *, estate_id: str, owner_id: str) -> dict[str, int | str]:
    estate = _require_estate_owner(db, estate_id, owner_id)
    if not getattr(estate, "join_code", None):
        estate.join_code = _generate_estate_join_code(db)
        db.commit()
        db.refresh(estate)
    return {
        "estateId": estate.id,
        "joinCode": estate.join_code or "",
        "reminderFrequencyDays": int(estate.reminder_frequency_days or 1),
        "canApproveWithoutHomeowner": bool(estate.security_can_approve_without_homeowner),
        "mustNotifyHomeowner": bool(estate.security_must_notify_homeowner),
        "requirePhotoVerification": bool(estate.security_require_photo_verification),
        "requireCallBeforeApproval": bool(estate.security_require_call_before_approval),
        "autoApproveTrustedVisitors": bool(estate.auto_approve_trusted_visitors),
        "suspiciousVisitWindowMinutes": int(estate.suspicious_visit_window_minutes or 20),
        "suspiciousHouseThreshold": int(estate.suspicious_house_threshold or 3),
        "suspiciousRejectionThreshold": int(estate.suspicious_rejection_threshold or 2),
    }


def update_estate_settings(
    db: Session,
    *,
    estate_id: str,
    owner_id: str,
    reminder_frequency_days: int,
    can_approve_without_homeowner: bool | None = None,
    must_notify_homeowner: bool | None = None,
    require_photo_verification: bool | None = None,
    require_call_before_approval: bool | None = None,
    auto_approve_trusted_visitors: bool | None = None,
    suspicious_visit_window_minutes: int | None = None,
    suspicious_house_threshold: int | None = None,
    suspicious_rejection_threshold: int | None = None,
) -> dict[str, int | str]:
    estate = _require_estate_owner(db, estate_id, owner_id)
    try:
        frequency_days = int(reminder_frequency_days)
    except (TypeError, ValueError):
        raise AppException("reminderFrequencyDays must be a number", status_code=400)
    if frequency_days < 1 or frequency_days > 365:
        raise AppException("reminderFrequencyDays must be between 1 and 365", status_code=400)
    estate.reminder_frequency_days = frequency_days
    if can_approve_without_homeowner is not None:
        estate.security_can_approve_without_homeowner = bool(can_approve_without_homeowner)
    if must_notify_homeowner is not None:
        estate.security_must_notify_homeowner = bool(must_notify_homeowner)
    if require_photo_verification is not None:
        estate.security_require_photo_verification = bool(require_photo_verification)
    if require_call_before_approval is not None:
        estate.security_require_call_before_approval = bool(require_call_before_approval)
    if auto_approve_trusted_visitors is not None:
        estate.auto_approve_trusted_visitors = bool(auto_approve_trusted_visitors)
    if suspicious_visit_window_minutes is not None:
        estate.suspicious_visit_window_minutes = max(5, int(suspicious_visit_window_minutes))
    if suspicious_house_threshold is not None:
        estate.suspicious_house_threshold = max(2, int(suspicious_house_threshold))
    if suspicious_rejection_threshold is not None:
        estate.suspicious_rejection_threshold = max(1, int(suspicious_rejection_threshold))
    db.commit()
    db.refresh(estate)
    return {
        "estateId": estate.id,
        "reminderFrequencyDays": int(estate.reminder_frequency_days or 1),
        "canApproveWithoutHomeowner": bool(estate.security_can_approve_without_homeowner),
        "mustNotifyHomeowner": bool(estate.security_must_notify_homeowner),
        "requirePhotoVerification": bool(estate.security_require_photo_verification),
        "requireCallBeforeApproval": bool(estate.security_require_call_before_approval),
        "autoApproveTrustedVisitors": bool(estate.auto_approve_trusted_visitors),
        "suspiciousVisitWindowMinutes": int(estate.suspicious_visit_window_minutes or 20),
        "suspiciousHouseThreshold": int(estate.suspicious_house_threshold or 3),
        "suspiciousRejectionThreshold": int(estate.suspicious_rejection_threshold or 2),
    }


def create_estate_homeowner(
    db: Session,
    owner_id: str,
    estate_id: str,
    full_name: str,
    email: str,
    password: str,
    unit_name: str | None = None,
    door_name: str | None = None,
) -> dict[str, Any]:
    require_subscription_feature(db, owner_id, "register_residents", user_role="estate")
    _require_estate_owner(db, estate_id, owner_id)
    subscription = get_effective_subscription(db, owner_id, user_role="estate")
    _enforce_home_limit(db, owner_id, subscription)

    email_clean = (email or "").strip().lower()
    full_name_clean = (full_name or "").strip()
    unit_name_clean = (unit_name or "").strip()
    door_name_clean = (door_name or "").strip()
    if not email_clean or not password or not full_name_clean:
        raise AppException("fullName, email and password are required", status_code=400)

    exists = db.query(User).filter(User.email == email_clean).first()
    if exists:
        raise AppException("Email already exists", status_code=409)

    user = User(
        full_name=full_name_clean,
        email=email_clean,
        password_hash=hash_password(password),
        role=UserRole.homeowner,
        email_verified=True,
        is_active=True,
        estate_id=estate_id,
    )
    db.add(user)
    db.flush()

    # Keep home/door records behind the scenes because the rest of the platform routes
    # visits, QR scans, alerts, and access logs through them.
    base_home_name = unit_name_clean or f"{full_name_clean} Home"
    home_name = base_home_name
    suffix = 2
    while db.query(Home).filter(Home.estate_id == estate_id, Home.name == home_name).first():
        home_name = f"{base_home_name} {suffix}"
        suffix += 1

    # Estate subscription capacity is based on houses/units only. Doors/gates
    # and QR records attached to a house do not create extra billable capacity.

    home = Home(name=home_name, estate_id=estate_id, homeowner_id=user.id)
    db.add(home)
    db.flush()

    resolved_door_name = door_name_clean or "Main Door"
    door = Door(name=resolved_door_name, home_id=home.id, is_active="online")
    db.add(door)
    db.flush()

    qr = _ensure_house_qr(db, estate_id=estate_id, home=home, door=door)

    db.commit()
    db.refresh(user)
    db.refresh(home)
    db.refresh(door)
    db.refresh(qr)
    return {
        "homeowner": user,
        "home": home,
        "door": door,
        "qr": qr,
    }


def create_estate_security_user(
    db: Session,
    *,
    owner_id: str,
    estate_id: str,
    full_name: str,
    email: str,
    password: str,
    phone: str | None = None,
    gate_id: str | None = None,
) -> User:
    _require_estate_owner(db, estate_id, owner_id)
    existing_security_count = (
        db.query(User)
        .filter(User.estate_id == estate_id, User.role == UserRole.security)
        .count()
    )
    require_subscription_feature(
        db,
        owner_id,
        "multiple_security_guards" if existing_security_count else "register_security_guards",
        user_role="estate",
    )

    clean_name = (full_name or "").strip()
    clean_email = (email or "").strip().lower()
    clean_phone = (phone or "").strip() or None
    clean_gate_id = (gate_id or "").strip() or None
    if not clean_name or not clean_email or not password:
        raise AppException("fullName, email and password are required", status_code=400)
    existing = db.query(User).filter(User.email == clean_email).first()
    if existing:
        raise AppException("Email already exists", status_code=409)

    user = User(
        full_name=clean_name,
        email=clean_email,
        password_hash=hash_password(password),
        role=UserRole.security,
        email_verified=True,
        is_active=True,
        phone=clean_phone,
        estate_id=estate_id,
        gate_id=clean_gate_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def list_estate_security_users(db: Session, *, owner_id: str, estate_id: str) -> list[dict[str, Any]]:
    _require_estate_owner(db, estate_id, owner_id)
    rows = (
        db.query(User)
        .filter(User.estate_id == estate_id, User.role == UserRole.security)
        .order_by(User.full_name.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "fullName": row.full_name,
            "email": row.email,
            "phone": row.phone,
            "gateId": row.gate_id,
            "estateId": row.estate_id,
            "active": bool(row.is_active),
            "status": "active" if row.is_active else "suspended",
        }
        for row in rows
    ]


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _to_money(value: Any) -> float:
    return float(value or 0)


def _append_timeline(items: list[dict[str, Any]], *, kind: str, label: str, at: Any, details: dict[str, Any] | None = None) -> None:
    if not at:
        return
    items.append({"kind": kind, "label": label, "at": _iso(at), "details": details or {}})


def get_estate_security_user_detail(db: Session, *, owner_id: str, estate_id: str, security_user_id: str) -> dict[str, Any]:
    estate = _require_estate_owner(db, estate_id, owner_id)
    guard = (
        db.query(User)
        .filter(User.id == security_user_id, User.estate_id == estate_id, User.role == UserRole.security)
        .first()
    )
    if not guard:
        raise AppException("Security account not found", status_code=404)

    attendance_rows = (
        db.query(GuardAttendance)
        .filter(GuardAttendance.estate_id == estate_id, GuardAttendance.guard_user_id == guard.id)
        .order_by(GuardAttendance.clock_in_at.desc())
        .limit(25)
        .all()
    )
    gate_rows = (
        db.query(GateLog, VisitorSession, Home)
        .outerjoin(VisitorSession, VisitorSession.id == GateLog.visitor_session_id)
        .outerjoin(Home, Home.id == GateLog.home_id)
        .filter(GateLog.estate_id == estate_id, GateLog.actor_user_id == guard.id)
        .order_by(GateLog.created_at.desc())
        .limit(60)
        .all()
    )
    incident_rows = (
        db.query(SecurityIncident)
        .filter(SecurityIncident.estate_id == estate_id, SecurityIncident.reported_by_user_id == guard.id)
        .order_by(SecurityIncident.created_at.desc())
        .limit(25)
        .all()
    )
    package_rows = (
        db.query(EstatePackage, Home)
        .join(Home, Home.id == EstatePackage.home_id)
        .filter(EstatePackage.estate_id == estate_id, EstatePackage.recorded_by_user_id == guard.id)
        .order_by(EstatePackage.arrived_at.desc())
        .limit(25)
        .all()
    )

    timeline: list[dict[str, Any]] = []
    for row in attendance_rows:
        _append_timeline(timeline, kind="attendance", label="Clocked in", at=row.clock_in_at, details={"gateId": row.gate_id, "status": row.status})
        _append_timeline(timeline, kind="attendance", label="Clocked out", at=row.clock_out_at, details={"gateId": row.gate_id, "status": "off_duty"})
    for log, session, home in gate_rows:
        label = {
            "security_confirm_entry": "Checked in visitor",
            "security_checkout": "Checked out visitor",
            "vehicle_entry": "Recorded vehicle entry",
            "vehicle_exit": "Recorded vehicle exit",
        }.get(log.action, log.action.replace("_", " ").title())
        _append_timeline(
            timeline,
            kind="gate",
            label=label,
            at=log.created_at,
            details={
                "gateId": log.gate_id,
                "house": home.name if home else "",
                "visitor": session.visitor_label if session else "",
                "status": log.resulting_status,
            },
        )
    for row in incident_rows:
        _append_timeline(timeline, kind="incident", label=f"Reported {row.incident_type}", at=row.created_at, details={"status": row.status, "gateId": row.gate_id})
    for package, home in package_rows:
        _append_timeline(timeline, kind="package", label="Registered package", at=package.arrived_at, details={"house": home.name, "status": package.status})
    timeline.sort(key=lambda item: item.get("at") or "", reverse=True)

    current_shift = next((row for row in attendance_rows if row.status == "on_duty" and not row.clock_out_at), None)
    return {
        "id": guard.id,
        "fullName": guard.full_name,
        "email": guard.email,
        "phone": guard.phone,
        "estateId": estate.id,
        "estateName": estate.name,
        "gateId": guard.gate_id,
        "active": bool(guard.is_active),
        "status": "active" if guard.is_active else "suspended",
        "createdAt": _iso(guard.created_at),
        "lastActivityAt": timeline[0]["at"] if timeline else None,
        "currentShift": {
            "status": current_shift.status,
            "gateId": current_shift.gate_id,
            "clockInAt": _iso(current_shift.clock_in_at),
        } if current_shift else None,
        "summary": {
            "attendanceRecords": len(attendance_rows),
            "visitorActions": len([row for row, _, _ in gate_rows if row.action not in {"vehicle_entry", "vehicle_exit"}]),
            "entriesConfirmed": len([row for row, _, _ in gate_rows if row.action == "security_confirm_entry"]),
            "checkoutsPerformed": len([row for row, _, _ in gate_rows if row.action == "security_checkout"]),
            "vehicleActions": len([row for row, _, _ in gate_rows if row.action in {"vehicle_entry", "vehicle_exit"}]),
            "incidentsReported": len(incident_rows),
            "packagesRegistered": len(package_rows),
        },
        "attendanceHistory": [
            {
                "id": row.id,
                "status": row.status,
                "gateId": row.gate_id,
                "clockInAt": _iso(row.clock_in_at),
                "clockOutAt": _iso(row.clock_out_at),
            }
            for row in attendance_rows
        ],
        "activityHistory": timeline[:80],
    }


def update_estate_security_user(
    db: Session,
    *,
    owner_id: str,
    estate_id: str,
    security_user_id: str,
    full_name: str,
    email: str,
    phone: str | None = None,
    gate_id: str | None = None,
    password: str | None = None,
) -> User:
    _require_estate_owner(db, estate_id, owner_id)
    require_subscription_feature(db, owner_id, "register_security_guards", user_role="estate")
    row = (
        db.query(User)
        .filter(User.id == security_user_id, User.estate_id == estate_id, User.role == UserRole.security)
        .first()
    )
    if not row:
        raise AppException("Security account not found", status_code=404)

    clean_name = (full_name or "").strip()
    clean_email = (email or "").strip().lower()
    clean_phone = (phone or "").strip() or None
    clean_gate_id = (gate_id or "").strip() or None
    if not clean_name or not clean_email:
        raise AppException("fullName and email are required", status_code=400)
    existing = db.query(User).filter(User.email == clean_email, User.id != row.id).first()
    if existing:
        raise AppException("Email already exists", status_code=409)

    row.full_name = clean_name
    row.email = clean_email
    row.phone = clean_phone
    row.gate_id = clean_gate_id
    if str(password or "").strip():
        row.password_hash = hash_password(password)
    db.commit()
    db.refresh(row)
    return row


def set_estate_security_user_active_state(
    db: Session,
    *,
    owner_id: str,
    estate_id: str,
    security_user_id: str,
    is_active: bool,
) -> User:
    _require_estate_owner(db, estate_id, owner_id)
    require_subscription_feature(db, owner_id, "register_security_guards", user_role="estate")
    row = (
        db.query(User)
        .filter(User.id == security_user_id, User.estate_id == estate_id, User.role == UserRole.security)
        .first()
    )
    if not row:
        raise AppException("Security account not found", status_code=404)
    row.is_active = bool(is_active)
    db.commit()
    db.refresh(row)
    return row


def delete_estate_security_user(
    db: Session,
    *,
    owner_id: str,
    estate_id: str,
    security_user_id: str,
) -> dict[str, Any]:
    _require_estate_owner(db, estate_id, owner_id)
    require_subscription_feature(db, owner_id, "register_security_guards", user_role="estate")
    row = (
        db.query(User)
        .filter(User.id == security_user_id, User.estate_id == estate_id, User.role == UserRole.security)
        .first()
    )
    if not row:
        raise AppException("Security account not found", status_code=404)
    deleted_id = row.id
    db.delete(row)
    db.commit()
    return {"id": deleted_id, "deleted": True}


def get_estate_resident_detail(db: Session, *, owner_id: str, estate_id: str, resident_id: str) -> dict[str, Any]:
    estate = _require_estate_owner(db, estate_id, owner_id)
    resident = db.query(User).filter(User.id == resident_id, User.role == UserRole.homeowner).first()
    if not resident:
        raise AppException("Resident not found", status_code=404)
    homes = (
        db.query(Home)
        .filter(Home.estate_id == estate_id, Home.homeowner_id == resident_id)
        .order_by(Home.created_at.desc())
        .all()
    )
    if not homes:
        raise AppException("Resident is not linked to this estate", status_code=404)
    home_ids = [home.id for home in homes]

    visitor_rows = (
        db.query(VisitorSession)
        .filter(VisitorSession.estate_id == estate_id, VisitorSession.homeowner_id == resident_id)
        .order_by(VisitorSession.started_at.desc())
        .limit(50)
        .all()
    )
    vehicle_rows = (
        db.query(ResidentVehicle)
        .filter(ResidentVehicle.estate_id == estate_id, ResidentVehicle.resident_id == resident_id)
        .order_by(ResidentVehicle.created_at.desc())
        .all()
    )
    vehicle_logs = (
        db.query(GateLog)
        .filter(GateLog.estate_id == estate_id, GateLog.home_id.in_(home_ids), GateLog.action.in_(["vehicle_entry", "vehicle_exit"]))
        .order_by(GateLog.created_at.desc())
        .limit(50)
        .all()
    )
    package_rows = (
        db.query(EstatePackage)
        .filter(EstatePackage.estate_id == estate_id, EstatePackage.resident_id == resident_id)
        .order_by(EstatePackage.arrived_at.desc())
        .limit(50)
        .all()
    )
    alert_rows = (
        db.query(EstateAlert)
        .filter(EstateAlert.estate_id == estate_id)
        .order_by(EstateAlert.created_at.desc())
        .limit(100)
        .all()
    )
    payment_rows = (
        db.query(HomeownerPayment, EstateAlert)
        .join(EstateAlert, EstateAlert.id == HomeownerPayment.estate_alert_id)
        .filter(EstateAlert.estate_id == estate_id, HomeownerPayment.homeowner_id == resident_id)
        .order_by(HomeownerPayment.created_at.desc())
        .limit(50)
        .all()
    )

    targeted_alerts = []
    for alert in alert_rows:
        if not alert.target_homeowner_ids:
            targeted_alerts.append(alert)
            continue
        try:
            target_ids = json.loads(alert.target_homeowner_ids) or []
        except Exception:
            target_ids = []
        if not target_ids or resident_id in target_ids:
            targeted_alerts.append(alert)

    timeline: list[dict[str, Any]] = []
    for row in visitor_rows:
        _append_timeline(timeline, kind="visitor", label=f"Visitor {row.visitor_label or 'Visitor'} {row.status}", at=row.started_at, details={"status": row.status, "gateId": row.gate_id})
    for row in package_rows:
        _append_timeline(timeline, kind="package", label=f"Package {row.status}", at=row.collected_at or row.arrived_at, details={"description": row.description, "status": row.status})
    for row in vehicle_logs:
        meta = {}
        try:
            meta = json.loads(row.meta_json or "{}")
        except Exception:
            meta = {}
        _append_timeline(
            timeline,
            kind="vehicle",
            label="Vehicle entered estate" if row.action == "vehicle_entry" else "Vehicle exited estate",
            at=row.created_at,
            details={"plateNumber": meta.get("plateNumber") or "", "gateId": row.gate_id},
        )
    for payment, alert in payment_rows:
        status = payment.status.value if hasattr(payment.status, "value") else str(payment.status)
        _append_timeline(timeline, kind="dues", label=f"Dues payment {status}", at=payment.paid_at or payment.created_at, details={"title": alert.title, "amount": _to_money(payment.amount_paid)})
    for alert in targeted_alerts:
        if alert.alert_type == EstateAlertType.maintenance_request:
            _append_timeline(timeline, kind="maintenance", label=f"Maintenance request {alert.maintenance_status or 'pending'}", at=alert.created_at, details={"title": alert.title})
    timeline.sort(key=lambda item: item.get("at") or "", reverse=True)

    dues = [alert for alert in targeted_alerts if alert.alert_type == EstateAlertType.payment_request]
    maintenance = [alert for alert in targeted_alerts if alert.alert_type == EstateAlertType.maintenance_request]
    paid_alert_ids = {payment.estate_alert_id for payment, _ in payment_rows if (payment.status.value if hasattr(payment.status, "value") else str(payment.status)) == "paid"}

    return {
        "id": resident.id,
        "personal": {
            "fullName": resident.full_name,
            "phone": resident.phone,
            "email": resident.email,
            "estateId": estate.id,
            "estateName": estate.name,
            "accountStatus": "active" if resident.is_active else "inactive",
            "createdAt": _iso(resident.created_at),
            "joinedAt": _iso(resident.created_at),
        },
        "household": {
            "homes": [{"id": home.id, "name": home.name, "estateId": home.estate_id, "createdAt": _iso(home.created_at)} for home in homes],
            "members": [],
            "trustedVisitors": [],
        },
        "visitors": {
            "total": db.query(VisitorSession).filter(VisitorSession.estate_id == estate_id, VisitorSession.homeowner_id == resident_id).count(),
            "recent": [
                {"id": row.id, "visitorName": row.visitor_label, "status": row.status, "gateId": row.gate_id, "startedAt": _iso(row.started_at), "endedAt": _iso(row.ended_at)}
                for row in visitor_rows[:10]
            ],
        },
        "maintenance": {
            "total": len(maintenance),
            "open": len([row for row in maintenance if (row.maintenance_status or "pending") != "solved"]),
            "closed": len([row for row in maintenance if (row.maintenance_status or "pending") == "solved"]),
            "recent": [{"id": row.id, "title": row.title, "status": row.maintenance_status or "pending", "createdAt": _iso(row.created_at)} for row in maintenance[:10]],
        },
        "dues": {
            "assigned": len(dues),
            "paid": len([row for row in dues if row.id in paid_alert_ids]),
            "outstanding": len([row for row in dues if row.id not in paid_alert_ids]),
            "history": [
                {
                    "id": payment.id,
                    "title": alert.title,
                    "status": payment.status.value if hasattr(payment.status, "value") else str(payment.status),
                    "amountPaid": _to_money(payment.amount_paid),
                    "paidAt": _iso(payment.paid_at),
                    "createdAt": _iso(payment.created_at),
                }
                for payment, alert in payment_rows
            ],
        },
        "packages": {
            "waiting": len([row for row in package_rows if row.status == "arrived"]),
            "collected": len([row for row in package_rows if row.status == "collected"]),
            "history": [{"id": row.id, "description": row.description, "status": row.status, "arrivedAt": _iso(row.arrived_at), "collectedAt": _iso(row.collected_at)} for row in package_rows],
        },
        "vehicles": {
            "registered": [
                {"id": row.id, "plateNumber": row.plate_number, "vehicleType": row.vehicle_type, "makeModel": row.make_model, "color": row.color, "active": row.is_active}
                for row in vehicle_rows
            ],
            "history": timeline[:50],
        },
        "auditTimeline": timeline[:100],
    }


def add_home(
    db: Session,
    name: str,
    estate_id: str | None,
    homeowner_id: str,
    owner_id: str | None = None,
) -> Home:
    home_name = (name or "").strip()
    if not home_name:
        raise AppException("Home name is required", status_code=400)
    if owner_id and estate_id:
        require_subscription_feature(db, owner_id, "register_residents", user_role="estate")
        _require_estate_owner(db, estate_id, owner_id)
        subscription = get_effective_subscription(db, owner_id, user_role="estate")
        _enforce_home_limit(db, owner_id, subscription)
    homeowner = db.query(User).filter(User.id == homeowner_id, User.role == UserRole.homeowner).first()
    if not homeowner:
        raise AppException("Homeowner not found", status_code=404)
    if estate_id:
        homeowner.estate_id = estate_id
    home = Home(name=home_name, estate_id=estate_id, homeowner_id=homeowner_id)
    db.add(home)
    db.commit()
    db.refresh(home)
    return home


def add_estate_door(
    db: Session,
    owner_id: str,
    estate_id: str,
    home_id: str,
    door_name: str,
    generate_qr: bool = True,
    mode: str = "direct",
    plan: str = "single",
) -> dict[str, Any]:
    require_subscription_feature(db, owner_id, "manual_visitor_logging", user_role="estate")
    _require_estate_owner(db, estate_id, owner_id)
    home = db.query(Home).filter(Home.id == home_id, Home.estate_id == estate_id).first()
    if not home:
        raise AppException("Home not found in estate", status_code=404)

    clean_name = (door_name or "").strip()
    if not clean_name:
        raise AppException("Door name is required", status_code=400)

    door = Door(name=clean_name, home_id=home.id, is_active="online")
    db.add(door)
    db.flush()

    qr_payload = None
    if generate_qr:
        qr = _ensure_house_qr(db, estate_id=estate_id, home=home, door=door)
        qr_payload = {
            "id": qr.id,
            "qrId": qr.qr_id,
            "scanUrl": f"/scan/{qr.qr_id}",
            "mode": qr.mode,
            "plan": qr.plan,
        }

    db.commit()
    db.refresh(door)
    return {
        "door": {"id": door.id, "name": door.name, "homeId": door.home_id, "state": "Online"},
        "qr": qr_payload,
    }


def provision_estate_door_with_homeowner(
    db: Session,
    owner_id: str,
    estate_id: str,
    home_name: str,
    door_name: str,
    homeowner_full_name: str,
    homeowner_username: str,
    homeowner_password: str,
) -> dict[str, Any]:
    created = create_estate_homeowner(
        db=db,
        owner_id=owner_id,
        estate_id=estate_id,
        full_name=homeowner_full_name,
        email=homeowner_username,  # Using username as email since function doesn't have email param
        password=homeowner_password,
        unit_name=home_name,
        door_name=door_name,
    )
    homeowner = created["homeowner"]
    home = created["home"]
    door = created["door"]
    qr = created["qr"]
    return {
        "homeowner": {"id": homeowner.id, "fullName": homeowner.full_name, "username": homeowner_username},
        "home": {"id": home.id, "name": home.name},
        "door": {"id": door.id, "name": door.name, "homeId": door.home_id, "state": "Online"},
        "qr": {
            "id": qr.id,
            "qrId": qr.qr_id,
            "scanUrl": f"/scan/{qr.qr_id}",
            "mode": qr.mode,
            "plan": qr.plan,
        },
    }


def assign_door_to_homeowner(db: Session, owner_id: str, door_id: str, homeowner_id: str) -> dict[str, Any]:
    require_subscription_feature(db, owner_id, "resident_management", user_role="estate")
    door_with_home = (
        db.query(Door, Home, Estate)
        .join(Home, Home.id == Door.home_id)
        .join(Estate, Estate.id == Home.estate_id)
        .filter(Door.id == door_id, Estate.owner_id == owner_id)
        .first()
    )
    if not door_with_home:
        raise AppException("Door not found for this estate", status_code=404)
    homeowner = db.query(User).filter(User.id == homeowner_id, User.role == UserRole.homeowner).first()
    if not homeowner:
        raise AppException("Homeowner not found", status_code=404)

    _, home, _ = door_with_home
    home.homeowner_id = homeowner_id
    db.add(
        Notification(
            user_id=homeowner_id,
            kind="estate.assignment",
            payload=f'{{"message":"A door was assigned to you in estate home {home.name}."}}',
        )
    )
    db.commit()
    return {"doorId": door_id, "homeownerId": homeowner_id, "homeId": home.id}


def invite_homeowner(
    db: Session,
    owner_id: str,
    homeowner_id: str,
    *,
    temporary_password: str | None = None,
    unit_name: str | None = None,
) -> dict[str, Any]:
    require_subscription_feature(db, owner_id, "register_residents", user_role="estate")
    homeowner = db.query(User).filter(User.id == homeowner_id, User.role == UserRole.homeowner).first()
    if not homeowner:
        raise AppException("Homeowner not found", status_code=404)

    homes = _estate_scope_homes_query(db, owner_id).filter(Home.homeowner_id == homeowner_id).all()
    if not homes:
        raise AppException("Homeowner is not linked to your estate", status_code=403)

    token = f"invite-{uuid.uuid4().hex[:10]}"
    login_link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/login"
    primary_home = homes[0] if homes else None
    estate = db.query(Estate).filter(Estate.id == primary_home.estate_id).first() if primary_home and primary_home.estate_id else None
    resident_name = homeowner.full_name or homeowner.email or "Resident"
    resolved_unit_name = (unit_name or (primary_home.name if primary_home else "")).strip() or "Assigned unit"
    estate_name = estate.name if estate else "your estate"
    clean_temporary_password = (temporary_password or "").strip() or None

    # If a temporary password is provided, update the homeowner's password hash
    # so they can login with the temporary password sent in the email
    if clean_temporary_password:
        homeowner.password_hash = hash_password(clean_temporary_password)
        db.add(homeowner)

    db.add(
        Notification(
            user_id=homeowner_id,
            kind="estate.invite",
            payload=f'{{"message":"Estate access invitation received.","inviteToken":"{token}"}}',
        )
    )
    db.commit()
    db.refresh(homeowner)  # Ensure homeowner email is fresh after commit

    try:
        send_push_fcm(
            db,
            user_id=homeowner_id,
            title="Estate Invitation",
            body="Estate access invitation received.",
            data={"kind": "estate.invite", "inviteToken": token},
        )
    except Exception:
        pass

    # Send invitation email with same pattern as OTP verification
    email_body = _build_estate_invite_email_body(
        estate_name=estate_name,
        resident_name=resident_name,
        unit_name=resolved_unit_name,
        email=homeowner.email,
        temporary_password=clean_temporary_password,
        login_link=login_link,
        invite_token=token,
    )

    delivery = send_transactional_email(
        to_email=homeowner.email,
        subject="Qring Estate Access Invitation",
        body=email_body,
    ) or {}

    email_status = str(delivery.get("status") or "unknown")
    email_reason = delivery.get("reason")
    email_message_id = delivery.get("messageId")

    return {
        "inviteToken": token,
        "sentAt": utc_now().isoformat(),
        "emailStatus": email_status,
        "emailReason": email_reason,
        "emailMessageId": email_message_id,
        "loginLink": login_link,
        "residentName": resident_name,
        "unitName": resolved_unit_name,
    }


def list_estate_mappings(db: Session, owner_id: str) -> list[dict[str, Any]]:
    homes = _estate_scope_homes_query(db, owner_id).order_by(Home.created_at.desc()).all()
    if not homes:
        return []
    home_ids = [home.id for home in homes]
    doors = db.query(Door).filter(Door.home_id.in_(home_ids)).all()
    homeowners = db.query(User).filter(User.id.in_({home.homeowner_id for home in homes})).all()
    homeowner_by_id = {user.id: user for user in homeowners}

    qr_rows = db.query(QRCode).filter(QRCode.home_id.in_(home_ids), QRCode.active.is_(True)).all()
    qr_by_door: dict[str, list[str]] = {}
    for qr in qr_rows:
        for door_id in [item.strip() for item in (qr.doors_csv or "").split(",") if item.strip()]:
            qr_by_door.setdefault(door_id, []).append(qr.qr_id)

    door_by_home: dict[str, list[Door]] = {}
    for door in doors:
        door_by_home.setdefault(door.home_id, []).append(door)

    return [
        {
            "homeId": home.id,
            "homeName": home.name,
            "homeownerId": home.homeowner_id,
            "homeownerName": homeowner_by_id.get(home.homeowner_id).full_name
            if homeowner_by_id.get(home.homeowner_id)
            else "",
            "homeownerEmail": homeowner_by_id.get(home.homeowner_id).email
            if homeowner_by_id.get(home.homeowner_id)
            else "",
            "doors": [
                {"id": door.id, "name": door.name, "qr": qr_by_door.get(door.id, [])}
                for door in door_by_home.get(home.id, [])
            ],
        }
        for home in homes
    ]


def list_estate_access_logs(db: Session, owner_id: str, limit: int = 100, category: str = "visitors") -> list[dict[str, Any]]:
    subscription = get_effective_subscription(db, owner_id, user_role="estate")
    cutoff = _limited_log_cutoff(subscription)
    normalized_category = (category or "visitors").strip().lower()
    estate_ids = [row.id for row in db.query(Estate.id).filter(Estate.owner_id == owner_id).all()]
    items: list[dict[str, Any]] = []

    if normalized_category in {"visitors", "all"}:
        rows = (
            db.query(VisitorSession, Door, Home)
            .join(Door, Door.id == VisitorSession.door_id)
            .join(Home, Home.id == Door.home_id)
            .join(Estate, Estate.id == Home.estate_id)
            .filter(Estate.owner_id == owner_id)
            .filter(VisitorSession.started_at >= cutoff if cutoff else True)
            .order_by(VisitorSession.started_at.desc())
            .limit(limit)
            .all()
        )
        items.extend({
            "id": session.id,
            "category": "visitor",
            "visitor": session.visitor_label,
            "status": session.status,
            "doorName": door.name,
            "homeName": home.name,
            "startedAt": session.started_at.isoformat(),
            "endedAt": session.ended_at.isoformat() if session.ended_at else None,
            "timestamp": session.started_at.isoformat() if session.started_at else None,
        } for session, door, home in rows)

    if normalized_category in {"vehicles", "all"} and estate_ids:
        require_subscription_feature(db, owner_id, "vehicle_entry_exit_records", user_role="estate")
        GuardUser = aliased(User)
        ResidentUser = aliased(User)
        vehicle_logs = (
            db.query(GateLog, Home, GuardUser, ResidentUser)
            .outerjoin(Home, Home.id == GateLog.home_id)
            .outerjoin(GuardUser, GuardUser.id == GateLog.actor_user_id)
            .outerjoin(ResidentUser, ResidentUser.id == Home.homeowner_id)
            .filter(GateLog.estate_id.in_(estate_ids), GateLog.action.in_(["vehicle_entry", "vehicle_exit"]))
            .order_by(GateLog.created_at.desc())
            .limit(limit)
            .all()
        )
        for log, home, guard, resident in vehicle_logs:
            meta = {}
            try:
                meta = json.loads(log.meta_json or "{}")
            except Exception:
                meta = {}
            action = "entry" if log.action == "vehicle_entry" else "exit"
            items.append(
                {
                    "id": log.id,
                    "category": "vehicle",
                    "vehiclePlate": meta.get("plateNumber") or "",
                    "vehicleId": meta.get("vehicleId") or "",
                    "action": action,
                    "status": action,
                    "gateId": log.gate_id,
                    "guardId": log.actor_user_id,
                    "guardName": guard.full_name if guard else "",
                    "homeId": log.home_id,
                    "homeName": home.name if home else "",
                    "residentName": resident.full_name if resident else "",
                    "notes": log.notes or "",
                    "timestamp": log.created_at.isoformat() if log.created_at else None,
                    "startedAt": log.created_at.isoformat() if log.created_at else None,
                    "endedAt": None,
                }
            )

    items.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
    return items[:limit]


def get_estate_plan_restrictions(db: Session, owner_id: str) -> dict[str, Any]:
    usage = _usage_for_owner(db, owner_id)
    effective_sub = get_effective_subscription(db, owner_id, user_role="estate")
    capacity = _estate_plan_capacity(effective_sub)
    used_estates = db.query(Estate).filter(Estate.owner_id == owner_id).count()
    included_houses = int(effective_sub.get("includedHouses") or capacity["maxHomes"] or 0)
    extra_house_amount = int(effective_sub.get("extraHouseAmount") or 0)
    extra_houses = max(0, usage["homes"] - included_houses) if included_houses > 0 else 0
    monthly_base_amount = int(effective_sub.get("amount") or effective_sub.get("monthlyAmount") or 0)
    extra_house_total = extra_houses * extra_house_amount

    return {
        "plan": effective_sub.get("plan", "free"),
        "planName": effective_sub.get("planName"),
        "status": effective_sub.get("status", "active"),
        "paymentStatus": effective_sub.get("paymentStatus", "unpaid"),
        "features": effective_sub.get("features", []),
        "featureFlags": effective_sub.get("featureFlags", {}),
        "restrictions": effective_sub.get("restrictions", []),
        "expiresAt": effective_sub.get("expiresAt"),
        "expiresSoon": bool(effective_sub.get("expiresSoon")),
        "trialDaysRemaining": int(effective_sub.get("trialDaysRemaining") or 0),
        "maxAdmins": int((effective_sub.get("limits") or {}).get("maxAdmins", 1) or 1),
        "maxEstates": capacity["maxEstates"],
        "maxHomes": capacity["maxHomes"],
        "includedHouses": included_houses,
        "extraHouseAmount": extra_house_amount,
        "extraHouses": extra_houses,
        "extraHouseMonthlyTotal": extra_house_total,
        "estimatedMonthlyTotal": monthly_base_amount + extra_house_total,
        "maxDoors": capacity["maxDoors"],
        "maxQrCodes": capacity["maxQrCodes"],
        "usedEstates": used_estates,
        "usedHomes": usage["homes"],
        "usedDoors": usage["doors"],
        "usedQrCodes": usage["qr_codes"],
        "remainingEstates": max(capacity["maxEstates"] - used_estates, 0) if capacity["maxEstates"] > 0 else 0,
        "remainingHomes": max(capacity["maxHomes"] - usage["homes"], 0) if capacity["maxHomes"] > 0 else 0,
        "remainingDoors": max(capacity["maxDoors"] - usage["doors"], 0),
        "remainingQrCodes": max(capacity["maxQrCodes"] - usage["qr_codes"], 0),
    }


def get_estate_stats_summary(db: Session, owner_id: str) -> dict[str, Any]:
    subscription = require_subscription_feature(db, owner_id, "analytics", user_role="estate")
    overview = list_estate_overview(db, owner_id)
    logs = list_estate_access_logs(db, owner_id, limit=300)
    total_visits = len(logs)
    approved = len([row for row in logs if "approved" in str(row.get("status") or "").lower()])
    rejected = len([row for row in logs if "rejected" in str(row.get("status") or "").lower()])
    return {
        "subscription": subscription,
        "summary": {
            "totalVisits": total_visits,
            "approved": approved,
            "rejected": rejected,
            "activeHomes": len(overview.get("homes") or []),
            "activeDoors": len(overview.get("doors") or []),
            "residents": len(overview.get("homeowners") or []),
        },
        "recentActivity": logs[:12],
    }


def create_estate_shared_selector_qr(db: Session, owner_id: str, estate_id: str) -> dict[str, Any]:
    _require_estate_owner(db, estate_id, owner_id)
    doors = (
        db.query(Door)
        .join(Home, Home.id == Door.home_id)
        .filter(Home.estate_id == estate_id)
        .order_by(Door.name.asc())
        .all()
    )
    if not doors:
        raise AppException(
            "No doors available for this estate. Create a homeowner/home and at least one door first.",
            status_code=400,
        )

    # If an active selector QR already exists for this estate, return it (idempotent)
    existing = (
        db.query(QRCode)
        .filter(QRCode.estate_id == estate_id, QRCode.mode == "selector", QRCode.active.is_(True))
        .order_by(QRCode.created_at.desc())
        .first()
    )
    if existing:
        return {
            "id": existing.id,
            "qrId": existing.qr_id,
            "scanUrl": f"/scan/{existing.qr_id}",
            "mode": existing.mode,
            "doorCount": len([d for d in (existing.doors_csv or "").split(",") if d.strip()]),
        }

    qr = QRCode(
        qr_id=f"qr-{uuid.uuid4().hex[:12]}",
        plan="multi",
        home_id=doors[0].home_id,
        doors_csv=",".join([door.id for door in doors]),
        mode="selector",
        estate_id=estate_id,
        active=True,
    )
    db.add(qr)
    try:
        db.commit()
    except IntegrityError:
        # Possible concurrent insert for same (estate_id, mode). Rollback and return existing row.
        db.rollback()
        existing_after = (
            db.query(QRCode)
            .filter(QRCode.estate_id == estate_id, QRCode.mode == "selector", QRCode.active.is_(True))
            .order_by(QRCode.created_at.desc())
            .first()
        )
        if existing_after:
            return {
                "id": existing_after.id,
                "qrId": existing_after.qr_id,
                "scanUrl": f"/scan/{existing_after.qr_id}",
                "mode": existing_after.mode,
                "doorCount": len([d for d in (existing_after.doors_csv or "").split(",") if d.strip()]),
            }
        raise
    db.refresh(qr)
    return {
        "id": qr.id,
        "qrId": qr.qr_id,
        "scanUrl": f"/scan/{qr.qr_id}",
        "mode": qr.mode,
        "doorCount": len(doors),
    }


def list_estate_shared_selector_qrs(db: Session, owner_id: str, estate_id: str) -> list[dict[str, Any]]:
    _require_estate_owner(db, estate_id, owner_id)
    rows = (
        db.query(QRCode)
        .filter(QRCode.estate_id == estate_id, QRCode.mode == "selector")
        .order_by(QRCode.created_at.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "qrId": row.qr_id,
            "scanUrl": f"/scan/{row.qr_id}",
            "mode": row.mode,
            "plan": row.plan,
            "active": bool(row.active),
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "doorCount": len([v for v in (row.doors_csv or "").split(",") if v.strip()]),
        }
        for row in rows
    ]


def update_estate_door_admin_profile(
    db: Session,
    owner_id: str,
    door_id: str,
    door_name: str | None = None,
    homeowner_name: str | None = None,
    homeowner_email: str | None = None,
    new_password: str | None = None,
) -> dict[str, Any]:
    row = (
        db.query(Door, Home, Estate, User)
        .join(Home, Home.id == Door.home_id)
        .join(Estate, Estate.id == Home.estate_id)
        .join(User, User.id == Home.homeowner_id)
        .filter(Door.id == door_id, Estate.owner_id == owner_id)
        .first()
    )
    if not row:
        raise AppException("Door not found for this estate", status_code=404)

    door, home, _, homeowner = row

    if door_name is not None:
        clean_door_name = door_name.strip()
        if not clean_door_name:
            raise AppException("Door name cannot be empty", status_code=400)
        door.name = clean_door_name

    if homeowner_name is not None:
        clean_homeowner_name = homeowner_name.strip()
        if not clean_homeowner_name:
            raise AppException("Homeowner name cannot be empty", status_code=400)
        homeowner.full_name = clean_homeowner_name

    if homeowner_email is not None:
        clean_email = homeowner_email.strip().lower()
        if not clean_email:
            raise AppException("Email cannot be empty", status_code=400)
        existing = db.query(User).filter(User.email == clean_email, User.id != homeowner.id).first()
        if existing:
            raise AppException("Email already in use", status_code=409)
        homeowner.email = clean_email

    if new_password is not None:
        if len(new_password) < 8:
            raise AppException("Password must be at least 8 characters", status_code=400)
        homeowner.password_hash = hash_password(new_password)

    db.commit()
    db.refresh(door)
    db.refresh(homeowner)

    return {
        "doorId": door.id,
        "doorName": door.name,
        "homeId": home.id,
        "homeName": home.name,
        "homeownerId": homeowner.id,
        "homeownerName": homeowner.full_name,
        "homeownerEmail": homeowner.email,
        "loginLink": f"{settings.FRONTEND_BASE_URL.rstrip('/')}/login",
    }
