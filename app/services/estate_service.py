from __future__ import annotations

import uuid
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Door, Estate, GateLog, Home, Notification, QRCode, User, UserRole, VisitorSession
from app.services.payment_service import get_effective_subscription, is_paid_subscription_expired, require_subscription_feature
from app.services.provider_integrations import send_email_smtp, send_push_fcm
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
        db.query(QRCode).filter(QRCode.home_id.in_(home_ids), QRCode.active.is_(True)).count()
        if home_ids
        else 0
    )
    return {
        "homes": len(home_ids),
        "doors": len(door_ids),
        "qr_codes": qr_count,
    }


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
    max_homes = _estate_plan_capacity(subscription)["maxHomes"]
    if max_homes <= 0:
        return
    used_homes = _estate_scope_homes_query(db, owner_id).count()
    if used_homes >= max_homes:
        raise AppException(
            f"Your {subscription.get('planName') or subscription.get('plan') or 'current'} plan supports only {max_homes} homes. Upgrade to add more units.",
            status_code=402,
        )


def _limited_log_cutoff(subscription: dict[str, Any]) -> datetime | None:
    retention_days = int(((subscription or {}).get("limits") or {}).get("logRetentionDays") or 0)
    if retention_days <= 0:
        return None
    return datetime.utcnow() - timedelta(days=retention_days)


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
                "homeownerId": row.resident_id,
                "homeownerName": homeowner_by_id[row.resident_id].full_name if row.resident_id in homeowner_by_id else "",
                "homeownerEmail": homeowner_by_id[row.resident_id].email if row.resident_id in homeowner_by_id else "",
                "homeownerRoleLabel": (
                    "Estate Homeowner"
                    if row.resident_id in homeowner_by_id and homeowner_by_id[row.resident_id].estate_id == row.estate_id
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
            "totalDailyVisitors": len([row for row in session_rows if row.started_at and row.started_at.date() == datetime.utcnow().date()]),
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
) -> User:
    _require_estate_owner(db, estate_id, owner_id)
    subscription = get_effective_subscription(db, owner_id, user_role="estate")
    _enforce_home_limit(db, owner_id, subscription)

    email_clean = (email or "").strip().lower()
    full_name_clean = (full_name or "").strip()
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

    # Ensure each created homeowner is persisted under the estate scope immediately.
    # This guarantees they appear in estate listings even before door assignment.
    base_home_name = f"{full_name_clean} Home"
    home_name = base_home_name
    suffix = 2
    while db.query(Home).filter(Home.estate_id == estate_id, Home.name == home_name).first():
        home_name = f"{base_home_name} {suffix}"
        suffix += 1
    db.add(Home(name=home_name, estate_id=estate_id, homeowner_id=user.id))

    db.commit()
    db.refresh(user)
    return user


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

    effective_sub = get_effective_subscription(db, owner_id)
    limits = effective_sub.get("limits", {})
    usage = _usage_for_owner(db, owner_id)
    max_doors = int(limits.get("maxDoors", 0) or 0)
    max_qr = int(limits.get("maxQrCodes", 0) or 0)
    if effective_sub.get("plan") == "free":
        max_doors = max(max_doors, FREE_ESTATE_LIMIT)
        max_qr = max(max_qr, FREE_ESTATE_LIMIT)

    if max_doors and usage["doors"] >= max_doors:
        raise AppException(f"Door limit reached ({max_doors})", status_code=402)

    door = Door(name=clean_name, home_id=home.id, is_active="online")
    db.add(door)
    db.flush()

    qr_payload = None
    if generate_qr:
        if max_qr and usage["qr_codes"] >= max_qr:
            raise AppException(f"QR limit reached ({max_qr})", status_code=402)
        qr = QRCode(
            qr_id=f"qr-{uuid.uuid4().hex[:12]}",
            plan=plan,
            home_id=home.id,
            doors_csv=door.id,
            mode=mode,
            estate_id=estate_id,
            active=True,
        )
        db.add(qr)
        db.flush()
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
    homeowner = create_estate_homeowner(
        db=db,
        owner_id=owner_id,
        estate_id=estate_id,
        full_name=homeowner_full_name,
        email=homeowner_username,  # Using username as email since function doesn't have email param
        password=homeowner_password,
    )
    home = add_home(
        db=db,
        name=home_name,
        estate_id=estate_id,
        homeowner_id=homeowner.id,
        owner_id=owner_id,
    )
    created = add_estate_door(
        db=db,
        owner_id=owner_id,
        estate_id=estate_id,
        home_id=home.id,
        door_name=door_name,
        generate_qr=True,
    )
    return {
        "homeowner": {"id": homeowner.id, "fullName": homeowner.full_name, "username": homeowner_username},
        "home": {"id": home.id, "name": home.name},
        **created,
    }


def assign_door_to_homeowner(db: Session, owner_id: str, door_id: str, homeowner_id: str) -> dict[str, Any]:
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

    delivery = send_email_smtp(
        to_email=homeowner.email,
        subject="Qring Estate Access Invitation",
        body=email_body,
    ) or {}

    email_status = str(delivery.get("status") or "unknown")
    email_reason = delivery.get("reason")
    email_message_id = delivery.get("messageId")

    return {
        "inviteToken": token,
        "sentAt": datetime.utcnow().isoformat(),
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


def list_estate_access_logs(db: Session, owner_id: str, limit: int = 100) -> list[dict[str, Any]]:
    subscription = get_effective_subscription(db, owner_id, user_role="estate")
    cutoff = _limited_log_cutoff(subscription)
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
    return [
        {
            "id": session.id,
            "visitor": session.visitor_label,
            "status": session.status,
            "doorName": door.name,
            "homeName": home.name,
            "startedAt": session.started_at.isoformat(),
            "endedAt": session.ended_at.isoformat() if session.ended_at else None,
        }
        for session, door, home in rows
    ]


def get_estate_plan_restrictions(db: Session, owner_id: str) -> dict[str, Any]:
    usage = _usage_for_owner(db, owner_id)
    effective_sub = get_effective_subscription(db, owner_id, user_role="estate")
    capacity = _estate_plan_capacity(effective_sub)
    used_estates = db.query(Estate).filter(Estate.owner_id == owner_id).count()

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

    effective_sub = get_effective_subscription(db, owner_id)
    limits = effective_sub.get("limits", {})
    max_qr = int(limits.get("maxQrCodes", 0) or 0)
    if effective_sub.get("plan") == "free":
        max_qr = max(max_qr, FREE_ESTATE_LIMIT)
    usage = _usage_for_owner(db, owner_id)
    if max_qr and usage["qr_codes"] >= max_qr:
        raise AppException(f"QR limit reached ({max_qr})", status_code=402)

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
    db.commit()
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
