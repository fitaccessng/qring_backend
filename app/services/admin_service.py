from sqlalchemy.orm import Session

from app.db.models import Door, Estate, Home, QRCode, Subscription, User, UserRole, VisitorSession
from app.services.payment_service import list_subscription_plans


def create_door(db: Session, name: str, home_id: str) -> Door:
    door = Door(name=name, home_id=home_id)
    db.add(door)
    db.commit()
    db.refresh(door)
    return door


def create_qr_code(
    db: Session,
    qr_id: str,
    plan: str,
    home_id: str,
    doors: list[str],
    mode: str,
    estate_id: str | None,
) -> QRCode:
    code = QRCode(
        qr_id=qr_id,
        plan=plan,
        home_id=home_id,
        doors_csv=",".join(doors),
        mode=mode,
        estate_id=estate_id,
        active=True,
    )
    db.add(code)
    db.commit()
    db.refresh(code)
    return code


def get_admin_overview(db: Session) -> dict:
    users = db.query(User).order_by(User.created_at.desc()).all()
    homeowners = [row for row in users if row.role == UserRole.homeowner]
    estates = [row for row in users if row.role == UserRole.estate]

    homes = db.query(Home).all()
    doors = db.query(Door).all()
    qr_codes = db.query(QRCode).all()
    sessions = db.query(VisitorSession).order_by(VisitorSession.started_at.desc()).all()
    subscriptions = db.query(Subscription).order_by(Subscription.starts_at.desc(), Subscription.id.desc()).all()

    plan_amount_by_id = {plan["id"]: int(plan["amount"]) for plan in list_subscription_plans(db, include_inactive=True)}
    total_payment_amount = sum(plan_amount_by_id.get(row.plan, 0) for row in subscriptions)

    homes_by_homeowner: dict[str, int] = {}
    homes_by_estate: dict[str, int] = {}
    for row in homes:
        homes_by_homeowner[row.homeowner_id] = homes_by_homeowner.get(row.homeowner_id, 0) + 1
        if row.estate_id:
            homes_by_estate[row.estate_id] = homes_by_estate.get(row.estate_id, 0) + 1

    home_ids_by_homeowner: dict[str, set[str]] = {}
    home_ids_by_estate: dict[str, set[str]] = {}
    for row in homes:
        home_ids_by_homeowner.setdefault(row.homeowner_id, set()).add(row.id)
        if row.estate_id:
            home_ids_by_estate.setdefault(row.estate_id, set()).add(row.id)

    doors_by_homeowner: dict[str, int] = {}
    doors_by_estate: dict[str, int] = {}
    home_by_id = {row.id: row for row in homes}
    for row in doors:
        home = home_by_id.get(row.home_id)
        if not home:
            continue
        doors_by_homeowner[home.homeowner_id] = doors_by_homeowner.get(home.homeowner_id, 0) + 1
        if home.estate_id:
            doors_by_estate[home.estate_id] = doors_by_estate.get(home.estate_id, 0) + 1

    qr_by_homeowner: dict[str, int] = {}
    qr_by_estate: dict[str, int] = {}
    for row in qr_codes:
        home = home_by_id.get(row.home_id)
        if not home:
            continue
        qr_by_homeowner[home.homeowner_id] = qr_by_homeowner.get(home.homeowner_id, 0) + 1
        if row.estate_id:
            qr_by_estate[row.estate_id] = qr_by_estate.get(row.estate_id, 0) + 1

    visits_by_homeowner: dict[str, dict] = {}
    for row in sessions:
        stats = visits_by_homeowner.setdefault(
            row.homeowner_id,
            {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "closed": 0},
        )
        stats["total"] += 1
        if row.status == "pending":
            stats["pending"] += 1
        elif row.status == "approved":
            stats["approved"] += 1
        elif row.status == "rejected":
            stats["rejected"] += 1
        elif row.status in {"closed", "completed"}:
            stats["closed"] += 1

    subscription_by_user: dict[str, Subscription] = {}
    for row in subscriptions:
        if row.user_id not in subscription_by_user:
            subscription_by_user[row.user_id] = row

    estate_rows = db.query(Estate).order_by(Estate.created_at.desc()).all()
    estate_by_id = {row.id: row for row in estate_rows}

    homeowner_details = [
        {
            "id": row.id,
            "fullName": row.full_name,
            "email": row.email,
            "active": row.is_active,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "homeCount": homes_by_homeowner.get(row.id, 0),
            "doorCount": doors_by_homeowner.get(row.id, 0),
            "qrCount": qr_by_homeowner.get(row.id, 0),
            "subscription": {
                "plan": subscription_by_user.get(row.id).plan if subscription_by_user.get(row.id) else "free",
                "status": subscription_by_user.get(row.id).status if subscription_by_user.get(row.id) else "active",
                "startsAt": (
                    subscription_by_user.get(row.id).starts_at.isoformat()
                    if subscription_by_user.get(row.id) and subscription_by_user.get(row.id).starts_at
                    else None
                ),
                "amount": plan_amount_by_id.get(subscription_by_user.get(row.id).plan, 0)
                if subscription_by_user.get(row.id)
                else 0,
            },
            "visits": visits_by_homeowner.get(
                row.id, {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "closed": 0}
            ),
        }
        for row in homeowners
    ]

    estate_details = []
    visits_by_estate_owner: dict[str, int] = {}
    for session in sessions:
        home = home_by_id.get(session.home_id)
        if not home or not home.estate_id:
            continue
        estate = estate_by_id.get(home.estate_id)
        if not estate:
            continue
        visits_by_estate_owner[estate.owner_id] = visits_by_estate_owner.get(estate.owner_id, 0) + 1

    for row in estates:
        owned_estates = [estate for estate in estate_rows if estate.owner_id == row.id]
        owned_estate_ids = {estate.id for estate in owned_estates}
        homeowner_count = len(
            {
                home.homeowner_id
                for home in homes
                if home.estate_id in owned_estate_ids and home.homeowner_id
            }
        )
        estate_details.append(
            {
                "id": row.id,
                "fullName": row.full_name,
                "email": row.email,
                "active": row.is_active,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
                "estateCount": len(owned_estates),
                "homeCount": sum(homes_by_estate.get(estate_id, 0) for estate_id in owned_estate_ids),
                "doorCount": sum(doors_by_estate.get(estate_id, 0) for estate_id in owned_estate_ids),
                "qrCount": sum(qr_by_estate.get(estate_id, 0) for estate_id in owned_estate_ids),
                "homeownerCount": homeowner_count,
                "visits": visits_by_estate_owner.get(row.id, 0),
                "subscription": {
                    "plan": subscription_by_user.get(row.id).plan if subscription_by_user.get(row.id) else "free",
                    "status": subscription_by_user.get(row.id).status if subscription_by_user.get(row.id) else "active",
                    "startsAt": (
                        subscription_by_user.get(row.id).starts_at.isoformat()
                        if subscription_by_user.get(row.id) and subscription_by_user.get(row.id).starts_at
                        else None
                    ),
                    "amount": plan_amount_by_id.get(subscription_by_user.get(row.id).plan, 0)
                    if subscription_by_user.get(row.id)
                    else 0,
                },
            }
        )

    homeowner_payment_history = [
        {
            "id": row.id,
            "userId": row.user_id,
            "userEmail": user.email if user else "",
            "userName": user.full_name if user else "",
            "plan": row.plan,
            "status": row.status,
            "amount": plan_amount_by_id.get(row.plan, 0),
            "startsAt": row.starts_at.isoformat() if row.starts_at else None,
            "endsAt": row.ends_at.isoformat() if row.ends_at else None,
        }
        for row in subscriptions
        for user in [next((u for u in homeowners if u.id == row.user_id), None)]
        if user
    ]

    estate_payment_history = [
        {
            "id": row.id,
            "userId": row.user_id,
            "userEmail": user.email if user else "",
            "userName": user.full_name if user else "",
            "plan": row.plan,
            "status": row.status,
            "amount": plan_amount_by_id.get(row.plan, 0),
            "startsAt": row.starts_at.isoformat() if row.starts_at else None,
            "endsAt": row.ends_at.isoformat() if row.ends_at else None,
        }
        for row in subscriptions
        for user in [next((u for u in estates if u.id == row.user_id), None)]
        if user
    ]

    visit_rows = []
    door_by_id = {row.id: row for row in doors}
    user_by_id = {row.id: row for row in users}
    for session in sessions[:300]:
        homeowner = user_by_id.get(session.homeowner_id)
        door = door_by_id.get(session.door_id)
        home = home_by_id.get(session.home_id)
        estate = estate_by_id.get(home.estate_id) if home and home.estate_id else None
        visit_rows.append(
            {
                "id": session.id,
                "visitor": session.visitor_label,
                "status": session.status,
                "homeownerId": session.homeowner_id,
                "homeownerName": homeowner.full_name if homeowner else "",
                "homeownerEmail": homeowner.email if homeowner else "",
                "estateId": estate.id if estate else None,
                "estateName": estate.name if estate else None,
                "doorId": session.door_id,
                "doorName": door.name if door else "",
                "startedAt": session.started_at.isoformat() if session.started_at else None,
                "endedAt": session.ended_at.isoformat() if session.ended_at else None,
            }
        )

    return {
        "metrics": {
            "totalHomeowners": len(homeowners),
            "totalEstates": len(estates),
            "totalUsers": len(users),
            "totalHomes": len(homes),
            "totalDoors": len(doors),
            "totalQrCodes": len(qr_codes),
            "totalVisits": len(sessions),
            "totalPaymentAmount": total_payment_amount,
        },
        "homeowners": homeowner_details,
        "estates": estate_details,
        "payments": {
            "totalAmount": total_payment_amount,
            "homeownerHistory": homeowner_payment_history,
            "estateHistory": estate_payment_history,
        },
        "visits": {
            "total": len(sessions),
            "rows": visit_rows,
        },
    }
