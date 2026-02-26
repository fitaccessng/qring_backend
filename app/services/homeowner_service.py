from collections import defaultdict
from datetime import datetime
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Door, Home, Message, QRCode, VisitorSession
from app.core.exceptions import AppException
from app.services.payment_service import get_effective_subscription, is_paid_subscription_expired

FREE_HOMEOWNER_LIMIT = 1
FREE_ESTATE_MANAGED_LIMIT = 5

STATUS_LABELS = {
    "pending": "Pending",
    "active": "Active",
    "approved": "Approved",
    "rejected": "Rejected",
    "closed": "Completed",
    "completed": "Completed",
}


def get_homeowner_context(db: Session, homeowner_id: str) -> dict[str, Any]:
    row = (
        db.query(Home)
        .filter(Home.homeowner_id == homeowner_id, Home.estate_id.is_not(None))
        .order_by(Home.created_at.desc())
        .first()
    )
    if not row or not row.estate_id:
        return {
            "managedByEstate": False,
            "estateId": None,
            "estateName": None,
            "estateOwnerId": None,
        }

    from app.db.models import Estate

    estate = db.query(Estate).filter(Estate.id == row.estate_id).first()
    return {
        "managedByEstate": bool(estate),
        "estateId": estate.id if estate else row.estate_id,
        "estateName": estate.name if estate else None,
        "estateOwnerId": estate.owner_id if estate else None,
    }


def _resolve_subscription_owner_id(db: Session, homeowner_id: str) -> str:
    context = get_homeowner_context(db, homeowner_id)
    return context.get("estateOwnerId") or homeowner_id


def list_homeowner_visits(db: Session, homeowner_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        db.query(VisitorSession, Door)
        .join(Door, Door.id == VisitorSession.door_id)
        .filter(VisitorSession.homeowner_id == homeowner_id)
        .order_by(VisitorSession.started_at.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id": session.id,
            "visitor": session.visitor_label or "Visitor",
            "door": door.name,
            "status": STATUS_LABELS.get(session.status, session.status.title()),
            "sessionStatus": session.status,
            "canDecide": session.status == "pending",
            "time": session.started_at.isoformat(),
        }
        for session, door in rows
    ]


def list_homeowner_message_threads(db: Session, homeowner_id: str, limit: int = 50) -> list[dict[str, Any]]:
    sessions = (
        db.query(VisitorSession, Door)
        .join(Door, Door.id == VisitorSession.door_id)
        .filter(VisitorSession.homeowner_id == homeowner_id)
        .order_by(VisitorSession.started_at.desc())
        .limit(limit)
        .all()
    )

    session_by_id = {session.id: (session, door) for session, door in sessions}
    if not session_by_id:
        return []

    messages = (
        db.query(Message)
        .filter(Message.session_id.in_(list(session_by_id.keys())))
        .order_by(Message.created_at.desc())
        .all()
    )

    latest_by_session: dict[str, Message] = {}
    unread_by_session: dict[str, int] = defaultdict(int)

    for message in messages:
        if message.session_id not in latest_by_session:
            latest_by_session[message.session_id] = message
        if message.sender_type != "homeowner" and message.read_by_homeowner_at is None:
            unread_by_session[message.session_id] += 1

    threads: list[dict[str, Any]] = []
    for session_id, (session, door) in session_by_id.items():
        latest = latest_by_session.get(session_id)
        if not latest:
            continue
        threads.append(
            {
                "id": session_id,
                "name": session.visitor_label or "Visitor",
                "door": door.name,
                "last": latest.body,
                "unread": unread_by_session.get(session_id, 0),
                "time": latest.created_at.isoformat(),
                "sessionStatus": session.status,
            }
        )

    threads.sort(key=lambda item: item["time"], reverse=True)
    return threads[:limit]


def list_homeowner_session_messages(
    db: Session, homeowner_id: str, session_id: str, limit: int = 300
) -> list[dict[str, Any]]:
    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.id == session_id, VisitorSession.homeowner_id == homeowner_id)
        .first()
    )
    if not session:
        return []

    # Opening a conversation marks all visitor messages in that session as read for homeowner.
    db.query(Message).filter(
        Message.session_id == session_id,
        Message.sender_type != "homeowner",
        Message.read_by_homeowner_at.is_(None),
    ).update(
        {Message.read_by_homeowner_at: datetime.utcnow()},
        synchronize_session=False,
    )
    db.commit()

    rows = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "sessionId": row.session_id,
            "text": row.body,
            "senderType": row.sender_type,
            "displayName": "Homeowner" if row.sender_type == "homeowner" else (session.visitor_label or "Visitor"),
            "at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def create_homeowner_session_message(
    db: Session, homeowner_id: str, session_id: str, text: str
) -> dict[str, Any] | None:
    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.id == session_id, VisitorSession.homeowner_id == homeowner_id)
        .first()
    )
    if not session:
        return None

    body = (text or "").strip()
    if not body:
        return None

    message = Message(
        session_id=session_id,
        sender_type="homeowner",
        body=body,
        created_at=datetime.utcnow(),
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return {
        "id": message.id,
        "sessionId": message.session_id,
        "text": message.body,
        "senderType": message.sender_type,
        "displayName": "Homeowner",
        "at": message.created_at.isoformat(),
    }


def delete_homeowner_session_message(
    db: Session, homeowner_id: str, session_id: str, message_id: str
) -> bool:
    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.id == session_id, VisitorSession.homeowner_id == homeowner_id)
        .first()
    )
    if not session:
        return False

    row = (
        db.query(Message)
        .filter(Message.id == message_id, Message.session_id == session_id)
        .first()
    )
    if not row:
        return False

    db.delete(row)
    db.commit()
    return True


def list_homeowner_doors(db: Session, homeowner_id: str) -> list[dict[str, Any]]:
    homes = db.query(Home).filter(Home.homeowner_id == homeowner_id).all()
    if not homes:
        return []

    home_ids = [home.id for home in homes]
    home_name_by_id = {home.id: home.name for home in homes}

    doors = db.query(Door).filter(Door.home_id.in_(home_ids)).order_by(Door.name.asc()).all()
    qr_codes = db.query(QRCode).filter(QRCode.home_id.in_(home_ids), QRCode.active.is_(True)).all()

    qr_by_door: dict[str, list[str]] = defaultdict(list)
    for qr in qr_codes:
        door_ids = [door_id.strip() for door_id in (qr.doors_csv or "").split(",") if door_id.strip()]
        for door_id in door_ids:
            qr_by_door[door_id].append(qr.qr_id)

    return [
        {
            "id": door.id,
            "name": door.name,
            "homeName": home_name_by_id.get(door.home_id, "Home"),
            "state": "Online" if door.is_active == "online" else "Offline",
            "qr": qr_by_door.get(door.id, []),
        }
        for door in doors
    ]


def get_homeowner_doors_data(db: Session, homeowner_id: str) -> dict[str, Any]:
    doors = list_homeowner_doors(db, homeowner_id)
    context = get_homeowner_context(db, homeowner_id)
    subscription_owner_id = _resolve_subscription_owner_id(db, homeowner_id)
    if context.get("managedByEstate") and subscription_owner_id and is_paid_subscription_expired(db, subscription_owner_id):
        db.query(QRCode).filter(QRCode.estate_id == context.get("estateId"), QRCode.active.is_(True)).update(
            {QRCode.active: False},
            synchronize_session=False,
        )
        db.commit()
        doors = list_homeowner_doors(db, homeowner_id)
    effective_sub = get_effective_subscription(db, subscription_owner_id)
    limits = effective_sub.get("limits", {})
    max_doors = int(limits.get("maxDoors", 0) or 0)
    max_qr_codes = int(limits.get("maxQrCodes", 0) or 0)
    if effective_sub.get("plan") == "free":
        floor = FREE_ESTATE_MANAGED_LIMIT if context.get("managedByEstate") else FREE_HOMEOWNER_LIMIT
        max_doors = max(max_doors, floor)
        max_qr_codes = max(max_qr_codes, floor)

    door_count = len(doors)
    qr_count = sum(len(door.get("qr", [])) for door in doors)

    return {
        "subscription": {
            "managedByEstate": context.get("managedByEstate", False),
            "estateId": context.get("estateId"),
            "estateName": context.get("estateName"),
            "subscriptionOwnerId": subscription_owner_id,
            "plan": effective_sub.get("plan", "free"),
            "status": effective_sub.get("status", "active"),
            "maxDoors": max_doors,
            "maxQrCodes": max_qr_codes,
            "usedDoors": door_count,
            "usedQrCodes": qr_count,
            "remainingDoors": max(max_doors - door_count, 0),
            "remainingQrCodes": max(max_qr_codes - qr_count, 0),
            "overDoorLimit": door_count > max_doors if max_doors > 0 else False,
            "overQrLimit": qr_count > max_qr_codes if max_qr_codes > 0 else False,
        },
        "doors": doors,
    }


def create_homeowner_door(
    db: Session,
    homeowner_id: str,
    name: str,
    generate_qr: bool = True,
    mode: str = "direct",
    qr_plan: str = "single",
) -> dict[str, Any]:
    context = get_homeowner_context(db, homeowner_id)
    if context.get("managedByEstate"):
        raise AppException("Estate-managed homeowners cannot create doors. Contact estate admin.", status_code=403)

    door_name = (name or "").strip()
    if not door_name:
        raise AppException("Door name is required", status_code=400)

    homes = db.query(Home).filter(Home.homeowner_id == homeowner_id).order_by(Home.created_at.asc()).all()
    if homes:
        home = homes[0]
    else:
        home = Home(name="Main Home", homeowner_id=homeowner_id)
        db.add(home)
        db.flush()
        homes = [home]

    home_ids = [row.id for row in homes]
    effective_sub = get_effective_subscription(db, homeowner_id)
    limits = effective_sub.get("limits", {})
    max_doors = int(limits.get("maxDoors", 0) or 0)
    max_qr_codes = int(limits.get("maxQrCodes", 0) or 0)
    if effective_sub.get("plan") == "free":
        max_doors = max(max_doors, FREE_HOMEOWNER_LIMIT)
        max_qr_codes = max(max_qr_codes, FREE_HOMEOWNER_LIMIT)

    total_doors = db.query(Door).filter(Door.home_id.in_(home_ids)).count() if home_ids else 0
    if max_doors and total_doors >= max_doors:
        raise AppException(
            f"Door limit reached ({max_doors}) for your {effective_sub.get('plan', 'current')} plan.",
            status_code=402,
        )

    door = Door(name=door_name, home_id=home.id, is_active="online")
    db.add(door)
    db.flush()

    created_qr = None
    if generate_qr:
        total_qr_codes = (
            db.query(QRCode).filter(QRCode.home_id.in_(home_ids), QRCode.active.is_(True)).count()
            if home_ids
            else 0
        )
        if max_qr_codes and total_qr_codes >= max_qr_codes:
            raise AppException(
                f"QR limit reached ({max_qr_codes}) for your {effective_sub.get('plan', 'current')} plan.",
                status_code=402,
            )

        qr = QRCode(
            qr_id=f"qr-{uuid.uuid4().hex[:12]}",
            plan=qr_plan,
            home_id=home.id,
            doors_csv=door.id,
            mode=mode,
            estate_id=home.estate_id,
            active=True,
        )
        db.add(qr)
        db.flush()
        created_qr = {
            "id": qr.id,
            "qr_id": qr.qr_id,
            "scan_url": f"/scan/{qr.qr_id}",
            "mode": qr.mode,
            "plan": qr.plan,
            "active": qr.active,
        }

    db.commit()
    db.refresh(door)

    return {
        "door": {
            "id": door.id,
            "name": door.name,
            "homeName": home.name,
            "state": "Online",
            "qr": [created_qr["qr_id"]] if created_qr else [],
        },
        "qr": created_qr,
    }


def generate_homeowner_door_qr(
    db: Session,
    homeowner_id: str,
    door_id: str,
    mode: str = "direct",
    plan: str = "single",
) -> dict[str, Any]:
    context = get_homeowner_context(db, homeowner_id)
    if context.get("managedByEstate"):
        raise AppException("Estate-managed homeowners cannot create QR codes. Contact estate admin.", status_code=403)

    effective_sub = get_effective_subscription(db, homeowner_id)
    limits = effective_sub.get("limits", {})
    max_doors = int(limits.get("maxDoors", 0) or 0)
    max_qr_codes = int(limits.get("maxQrCodes", 0) or 0)

    homes = db.query(Home).filter(Home.homeowner_id == homeowner_id).all()
    home_ids = [home.id for home in homes]
    total_doors = db.query(Door).filter(Door.home_id.in_(home_ids)).count() if home_ids else 0
    total_qr_codes = (
        db.query(QRCode).filter(QRCode.home_id.in_(home_ids), QRCode.active.is_(True)).count()
        if home_ids
        else 0
    )

    if max_doors and total_doors > max_doors:
        raise AppException(
            f"Your {effective_sub.get('plan', 'current')} plan supports up to {max_doors} doors. Upgrade required.",
            status_code=402,
        )
    if max_qr_codes and total_qr_codes >= max_qr_codes:
        raise AppException(
            f"QR limit reached ({max_qr_codes}) for your {effective_sub.get('plan', 'current')} plan.",
            status_code=402,
        )

    row = (
        db.query(Door, Home)
        .join(Home, Home.id == Door.home_id)
        .filter(Door.id == door_id, Home.homeowner_id == homeowner_id)
        .first()
    )
    if not row:
        raise AppException("Door not found for homeowner", status_code=404)

    door, home = row
    qr_id = f"qr-{uuid.uuid4().hex[:12]}"

    qr = QRCode(
        qr_id=qr_id,
        plan=plan,
        home_id=home.id,
        doors_csv=door.id,
        mode=mode,
        estate_id=home.estate_id,
        active=True,
    )
    db.add(qr)
    db.commit()
    db.refresh(qr)

    return {
        "id": qr.id,
        "qr_id": qr.qr_id,
        "door_id": door.id,
        "scan_url": f"/scan/{qr.qr_id}",
        "mode": qr.mode,
        "plan": qr.plan,
        "active": qr.active,
    }
