import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Door, Estate, Home, Notification, QRCode, User, UserRole, VisitorSession
from app.services.payment_service import get_effective_subscription, is_paid_subscription_expired
settings = get_settings()


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
        db.query(User).filter(User.id.in_(homeowner_ids), User.role == UserRole.homeowner).all() if homeowner_ids else []
    )
    homeowner_by_id = {user.id: user for user in homeowners}
    home_by_id = {home.id: home for home in homes}

    qr_rows = db.query(QRCode).filter(QRCode.home_id.in_(home_ids), QRCode.active.is_(True)).all() if home_ids else []
    qr_by_door: dict[str, list[str]] = {}
    for qr in qr_rows:
        for door_id in [item.strip() for item in (qr.doors_csv or "").split(",") if item.strip()]:
            qr_by_door.setdefault(door_id, []).append(qr.qr_id)

    usage = _usage_for_owner(db, owner_id)
    limits = get_effective_subscription(db, owner_id).get("limits", {})
    max_doors = int(limits.get("maxDoors", 0) or 0)
    max_qr_codes = int(limits.get("maxQrCodes", 0) or 0)

    return {
        "estates": [{"id": row.id, "name": row.name, "createdAt": row.created_at.isoformat()} for row in estates],
        "homes": [
            {
                "id": row.id,
                "name": row.name,
                "estateId": row.estate_id,
                "homeownerId": row.homeowner_id,
                "homeownerName": homeowner_by_id[row.homeowner_id].full_name if row.homeowner_id in homeowner_by_id else "",
                "homeownerEmail": homeowner_by_id[row.homeowner_id].email if row.homeowner_id in homeowner_by_id else "",
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
            {"id": row.id, "fullName": row.full_name, "email": row.email, "active": row.is_active}
            for row in homeowners
        ],
        "planRestrictions": {
            "maxDoors": max_doors,
            "maxQrCodes": max_qr_codes,
            "usedDoors": usage["doors"],
            "usedQrCodes": usage["qr_codes"],
            "remainingDoors": max(max_doors - usage["doors"], 0),
            "remainingQrCodes": max(max_qr_codes - usage["qr_codes"], 0),
        },
    }


def create_estate(db: Session, name: str, owner_id: str) -> Estate:
    estate_name = (name or "").strip()
    if not estate_name:
        raise AppException("Estate name is required", status_code=400)
    estate = Estate(name=estate_name, owner_id=owner_id)
    db.add(estate)
    db.commit()
    db.refresh(estate)
    return estate


def create_estate_homeowner(
    db: Session,
    owner_id: str,
    estate_id: str,
    full_name: str,
    username: str,
    password: str,
) -> User:
    _require_estate_owner(db, estate_id, owner_id)

    username_clean = (username or "").strip().lower()
    full_name_clean = (full_name or "").strip()
    if not username_clean or not password or not full_name_clean:
        raise AppException("fullName, username and password are required", status_code=400)

    email = username_clean if "@" in username_clean else f"{username_clean}@estate.useqring.online"
    exists = db.query(User).filter(User.email == email).first()
    if exists:
        raise AppException("Username already exists", status_code=409)

    user = User(
        full_name=full_name_clean,
        email=email,
        password_hash=hash_password(password),
        role=UserRole.homeowner,
        email_verified=True,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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
    homeowner = db.query(User).filter(User.id == homeowner_id, User.role == UserRole.homeowner).first()
    if not homeowner:
        raise AppException("Homeowner not found", status_code=404)
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
        max_doors = max(max_doors, 1)
        max_qr = max(max_qr, 1)

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
        username=homeowner_username,
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


def invite_homeowner(db: Session, owner_id: str, homeowner_id: str) -> dict[str, Any]:
    homeowner = db.query(User).filter(User.id == homeowner_id, User.role == UserRole.homeowner).first()
    if not homeowner:
        raise AppException("Homeowner not found", status_code=404)

    homes = _estate_scope_homes_query(db, owner_id).filter(Home.homeowner_id == homeowner_id).all()
    if not homes:
        raise AppException("Homeowner is not linked to your estate", status_code=403)

    token = f"invite-{uuid.uuid4().hex[:10]}"
    db.add(
        Notification(
            user_id=homeowner_id,
            kind="estate.invite",
            payload=f'{{"message":"Estate access invitation received.","inviteToken":"{token}"}}',
        )
    )
    db.commit()
    return {"inviteToken": token, "sentAt": datetime.utcnow().isoformat()}


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
    rows = (
        db.query(VisitorSession, Door, Home)
        .join(Door, Door.id == VisitorSession.door_id)
        .join(Home, Home.id == Door.home_id)
        .join(Estate, Estate.id == Home.estate_id)
        .filter(Estate.owner_id == owner_id)
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
    effective_sub = get_effective_subscription(db, owner_id)
    limits = effective_sub.get("limits", {})
    max_doors = int(limits.get("maxDoors", 0) or 0)
    max_qr_codes = int(limits.get("maxQrCodes", 0) or 0)

    return {
        "plan": effective_sub.get("plan", "free"),
        "status": effective_sub.get("status", "active"),
        "maxDoors": max_doors,
        "maxQrCodes": max_qr_codes,
        "usedDoors": usage["doors"],
        "usedQrCodes": usage["qr_codes"],
        "remainingDoors": max(max_doors - usage["doors"], 0),
        "remainingQrCodes": max(max_qr_codes - usage["qr_codes"], 0),
    }


def create_estate_shared_selector_qr(db: Session, owner_id: str, estate_id: str) -> dict[str, Any]:
    _require_estate_owner(db, estate_id, owner_id)
    homes = db.query(Home).filter(Home.estate_id == estate_id).all()
    home_ids = [home.id for home in homes]
    if not home_ids:
        raise AppException("No homes configured for this estate", status_code=400)

    doors = db.query(Door).filter(Door.home_id.in_(home_ids)).order_by(Door.name.asc()).all()
    if not doors:
        raise AppException("No doors available for this estate", status_code=400)

    limits = get_effective_subscription(db, owner_id).get("limits", {})
    max_qr = int(limits.get("maxQrCodes", 0) or 0)
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
