from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.core.config import get_settings
from app.db.models import Door, Estate, Home, Message, Notification, QRCode, Subscription, SubscriptionPlan, User, UserRole, VisitorSession
from app.db.session import get_db
from app.services.admin_service import create_door, create_qr_code, get_admin_overview
from app.services.payment_service import list_subscription_plans, upsert_plan
from app.services.audit_service import list_audit_logs, write_audit_log

router = APIRouter()
settings = get_settings()


class DoorCreate(BaseModel):
    name: str
    homeId: str


class QRCreate(BaseModel):
    qrId: str
    plan: str
    homeId: str
    doors: list[str]
    mode: str
    estateId: str | None = None


class PlanUpsert(BaseModel):
    id: str
    name: str
    amount: int = 0
    currency: str = "NGN"
    maxDoors: int = 1
    maxQrCodes: int = 1
    active: bool = True


class UserPatch(BaseModel):
    isActive: bool | None = None


class SubscriptionActivate(BaseModel):
    userId: str
    plan: str


@router.post("/doors")
def admin_create_door(
    payload: DoorCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    door = create_door(db, payload.name, payload.homeId)
    return {"data": {"id": door.id, "name": door.name, "homeId": door.home_id}}


@router.post("/qrs")
def admin_create_qr(
    payload: QRCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    code = create_qr_code(
        db=db,
        qr_id=payload.qrId,
        plan=payload.plan,
        home_id=payload.homeId,
        doors=payload.doors,
        mode=payload.mode,
        estate_id=payload.estateId,
    )
    return {"data": {"id": code.id, "qrId": code.qr_id}}


@router.get("/overview")
def admin_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    return {"data": get_admin_overview(db)}


@router.get("/plans")
def admin_list_plans(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    return {"data": list_subscription_plans(db, include_inactive=True)}


@router.put("/plans/{plan_id}")
def admin_update_plan(
    plan_id: str,
    payload: PlanUpsert,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    # Keep URL as the source of truth for the plan id.
    row = upsert_plan(
        db=db,
        plan_id=plan_id,
        name=payload.name,
        amount=payload.amount,
        currency=payload.currency,
        max_doors=payload.maxDoors,
        max_qr_codes=payload.maxQrCodes,
        active=payload.active,
    )
    return {
        "data": {
            "id": row.id,
            "name": row.name,
            "amount": int(row.amount or 0),
            "currency": row.currency,
            "maxDoors": int(row.max_doors or 0),
            "maxQrCodes": int(row.max_qr_codes or 0),
            "active": bool(row.active),
        }
    }


@router.get("/users")
def admin_list_users(
    role: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    query = db.query(User).order_by(User.created_at.desc())
    if role:
        query = query.filter(User.role == UserRole(role))
    if q:
        term = f"%{q.strip().lower()}%"
        query = query.filter((User.email.ilike(term)) | (User.full_name.ilike(term)))
    rows = query.limit(limit).all()
    return {
        "data": [
            {
                "id": row.id,
                "fullName": row.full_name,
                "email": row.email,
                "role": row.role.value,
                "active": bool(row.is_active),
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.patch("/users/{user_id}")
def admin_patch_user(
    user_id: str,
    payload: UserPatch,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("admin")),
):
    row = db.query(User).filter(User.id == user_id).first()
    if not row:
        return {"data": None}
    if payload.isActive is not None:
        row.is_active = bool(payload.isActive)
    db.commit()
    write_audit_log(db, actor_user_id=actor.id, action="user.patch", resource_type="user", resource_id=row.id, meta={"isActive": payload.isActive})
    return {
        "data": {
            "id": row.id,
            "fullName": row.full_name,
            "email": row.email,
            "role": row.role.value,
            "active": bool(row.is_active),
        }
    }


@router.get("/estates")
def admin_list_estates(
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    estates = db.query(Estate).order_by(Estate.created_at.desc()).limit(limit).all()
    owners = db.query(User).filter(User.id.in_([e.owner_id for e in estates])).all() if estates else []
    owner_by_id = {o.id: o for o in owners}
    return {
        "data": [
            {
                "id": row.id,
                "name": row.name,
                "ownerId": row.owner_id,
                "ownerEmail": owner_by_id.get(row.owner_id).email if owner_by_id.get(row.owner_id) else "",
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in estates
        ]
    }


@router.get("/doors/all")
def admin_list_doors(
    limit: int = Query(default=300, ge=1, le=1000),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    doors = db.query(Door).limit(limit).all()
    homes = db.query(Home).filter(Home.id.in_([d.home_id for d in doors])).all() if doors else []
    home_by_id = {h.id: h for h in homes}
    homeowners = db.query(User).filter(User.id.in_([h.homeowner_id for h in homes])).all() if homes else []
    homeowner_by_id = {u.id: u for u in homeowners}
    estates = db.query(Estate).filter(Estate.id.in_([h.estate_id for h in homes if h.estate_id])).all() if homes else []
    estate_by_id = {e.id: e for e in estates}
    return {
        "data": [
            {
                "id": d.id,
                "name": d.name,
                "state": d.is_active,
                "homeId": d.home_id,
                "homeName": home_by_id.get(d.home_id).name if home_by_id.get(d.home_id) else "",
                "homeownerId": home_by_id.get(d.home_id).homeowner_id if home_by_id.get(d.home_id) else "",
                "homeownerEmail": homeowner_by_id.get(home_by_id.get(d.home_id).homeowner_id).email
                if home_by_id.get(d.home_id) and homeowner_by_id.get(home_by_id.get(d.home_id).homeowner_id)
                else "",
                "estateId": home_by_id.get(d.home_id).estate_id if home_by_id.get(d.home_id) else None,
                "estateName": estate_by_id.get(home_by_id.get(d.home_id).estate_id).name
                if home_by_id.get(d.home_id) and home_by_id.get(d.home_id).estate_id and estate_by_id.get(home_by_id.get(d.home_id).estate_id)
                else None,
            }
            for d in doors
        ]
    }


@router.get("/qrs/all")
def admin_list_qrs(
    limit: int = Query(default=300, ge=1, le=1000),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    rows = db.query(QRCode).order_by(QRCode.created_at.desc()).limit(limit).all()
    return {
        "data": [
            {
                "id": row.id,
                "qrId": row.qr_id,
                "mode": row.mode,
                "plan": row.plan,
                "homeId": row.home_id,
                "estateId": row.estate_id,
                "active": bool(row.active),
                "doorCount": len([v for v in (row.doors_csv or "").split(",") if v.strip()]),
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.get("/subscriptions")
def admin_list_subscriptions(
    limit: int = Query(default=300, ge=1, le=1000),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    rows = db.query(Subscription).order_by(Subscription.starts_at.desc(), Subscription.id.desc()).limit(limit).all()
    user_ids = list({r.user_id for r in rows})
    users = db.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []
    user_by_id = {u.id: u for u in users}
    return {
        "data": [
            {
                "id": row.id,
                "userId": row.user_id,
                "userEmail": user_by_id.get(row.user_id).email if user_by_id.get(row.user_id) else "",
                "userRole": user_by_id.get(row.user_id).role.value if user_by_id.get(row.user_id) else "",
                "plan": row.plan,
                "status": row.status,
                "startsAt": row.starts_at.isoformat() if row.starts_at else None,
                "endsAt": row.ends_at.isoformat() if row.ends_at else None,
            }
            for row in rows
        ]
    }


@router.post("/subscriptions/activate")
def admin_activate_subscription(
    payload: SubscriptionActivate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("admin")),
):
    # Minimal activation: insert a subscription row. Limits are resolved by /payment/subscription/me.
    plan_row = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == payload.plan).first()
    if not plan_row:
        return {"data": None}
    row = Subscription(user_id=payload.userId, plan=payload.plan, status="active")
    db.add(row)
    db.commit()
    db.refresh(row)
    write_audit_log(db, actor_user_id=actor.id, action="subscription.activate", resource_type="subscription", resource_id=row.id, meta={"userId": payload.userId, "plan": payload.plan})
    return {"data": {"id": row.id, "userId": row.user_id, "plan": row.plan, "status": row.status}}


@router.get("/payments")
def admin_payments_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    # Payments are derived from subscription rows in this app.
    subs = db.query(Subscription).all()
    return {"data": {"count": len(subs)}}


@router.get("/messages")
def admin_list_messages(
    sessionId: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    q = db.query(Message).order_by(Message.created_at.desc())
    if sessionId:
        q = q.filter(Message.session_id == sessionId)
    rows = q.limit(limit).all()
    return {
        "data": [
            {
                "id": row.id,
                "sessionId": row.session_id,
                "senderType": row.sender_type,
                "body": row.body,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.delete("/messages/{message_id}")
def admin_delete_message(
    message_id: str,
    db: Session = Depends(get_db),
    actor: User = Depends(require_roles("admin")),
):
    row = db.query(Message).filter(Message.id == message_id).first()
    if not row:
        return {"data": {"deleted": False}}
    db.delete(row)
    db.commit()
    write_audit_log(db, actor_user_id=actor.id, action="message.delete", resource_type="message", resource_id=message_id)
    return {"data": {"deleted": True}}


@router.get("/notifications")
def admin_list_notifications(
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    rows = db.query(Notification).order_by(Notification.created_at.desc()).limit(limit).all()
    return {
        "data": [
            {
                "id": row.id,
                "userId": row.user_id,
                "kind": row.kind,
                "payload": row.payload,
                "readAt": row.read_at.isoformat() if row.read_at else None,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.get("/sessions")
def admin_list_sessions(
    status: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    q = db.query(VisitorSession).order_by(VisitorSession.started_at.desc())
    if status:
        q = q.filter(VisitorSession.status == status)
    rows = q.limit(limit).all()
    return {
        "data": [
            {
                "id": row.id,
                "qrId": row.qr_id,
                "homeId": row.home_id,
                "doorId": row.door_id,
                "homeownerId": row.homeowner_id,
                "visitor": row.visitor_label,
                "status": row.status,
                "startedAt": row.started_at.isoformat() if row.started_at else None,
                "endedAt": row.ended_at.isoformat() if row.ended_at else None,
            }
            for row in rows
        ]
    }


@router.get("/analytics")
def admin_analytics(
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    return {"data": get_admin_overview(db).get("metrics", {})}


@router.get("/config")
def admin_config(
    _: User = Depends(require_roles("admin")),
):
    return {
        "data": {
            "environment": settings.ENVIRONMENT,
            "debug": bool(settings.DEBUG),
            "paystackConfigured": bool(settings.PAYSTACK_SECRET_KEY and settings.PAYSTACK_PUBLIC_KEY),
            "vapidConfigured": bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY),
            "adminSignupKeySet": bool(settings.ADMIN_SIGNUP_KEY),
            "frontendBaseUrl": settings.FRONTEND_BASE_URL,
        }
    }


@router.get("/audit")
def admin_audit(
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    rows = list_audit_logs(db, limit=limit)
    return {
        "data": [
            {
                "id": row.id,
                "actorUserId": row.actor_user_id,
                "action": row.action,
                "resourceType": row.resource_type,
                "resourceId": row.resource_id,
                "meta": row.meta_json,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }
