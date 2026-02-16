from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import Door, Estate, Home, QRCode, User
from app.services.payment_service import is_paid_subscription_expired


def resolve_qr(db: Session, qr_id: str) -> dict:
    qr = db.query(QRCode).filter(QRCode.qr_id == qr_id).first()
    if not qr or not qr.active:
        raise AppException("QR not found or inactive", status_code=404)

    if qr.estate_id:
        estate = db.query(Estate).filter(Estate.id == qr.estate_id).first()
        if estate and is_paid_subscription_expired(db, estate.owner_id):
            db.query(QRCode).filter(QRCode.estate_id == qr.estate_id, QRCode.active.is_(True)).update(
                {QRCode.active: False},
                synchronize_session=False,
            )
            db.commit()
            raise AppException("Estate subscription expired. QR codes are inactive.", status_code=402)

    door_ids = [d.strip() for d in qr.doors_csv.split(",") if d.strip()]
    rows = (
        db.query(Door, Home, User)
        .join(Home, Home.id == Door.home_id)
        .join(User, User.id == Home.homeowner_id)
        .filter(Door.id.in_(door_ids))
        .all()
        if door_ids
        else []
    )
    door_index = {door.id: (door, home, user) for door, home, user in rows}
    door_options = []
    for door_id in door_ids:
        door, home, user = door_index.get(door_id, (None, None, None))
        if not door:
            continue
        door_options.append(
            {
                "id": door.id,
                "name": door.name,
                "homeId": home.id if home else "",
                "homeName": home.name if home else "",
                "homeownerId": user.id if user else "",
                "homeownerName": user.full_name if user else "",
            }
        )

    return {
        "qr_id": qr.qr_id,
        "plan": qr.plan,
        "home_id": qr.home_id,
        "doors": [item["id"] for item in door_options] if door_options else door_ids,
        "doorOptions": door_options,
        "mode": qr.mode,
        "estate_id": qr.estate_id,
        "active": qr.active,
    }
