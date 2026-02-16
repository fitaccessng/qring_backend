import json
import hmac
import uuid
from datetime import datetime, timedelta
from hashlib import sha512
from urllib import error, request

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import PaymentPurpose, Subscription, SubscriptionPlan

settings = get_settings()

DEFAULT_PLAN_CATALOG = [
    {"id": "free", "name": "Free", "amount": 0, "currency": "NGN", "maxDoors": 1, "maxQrCodes": 1},
    {"id": "doors_20", "name": "1-20 doors", "amount": 20000, "currency": "NGN", "maxDoors": 20, "maxQrCodes": 20},
    {"id": "doors_40", "name": "1-40 doors", "amount": 50000, "currency": "NGN", "maxDoors": 40, "maxQrCodes": 40},
    {"id": "doors_80", "name": "1-80 doors", "amount": 80000, "currency": "NGN", "maxDoors": 80, "maxQrCodes": 80},
    {"id": "doors_100", "name": "1-100 doors", "amount": 120000, "currency": "NGN", "maxDoors": 100, "maxQrCodes": 100},
]


def create_payment_purpose(db: Session, name: str, description: str, account_info: str):
    purpose = PaymentPurpose(name=name, description=description, account_info=account_info)
    db.add(purpose)
    db.commit()
    db.refresh(purpose)
    return purpose


def activate_subscription(db: Session, user_id: str, plan: str):
    row = Subscription(
        user_id=user_id,
        plan=plan,
        status="active",
        starts_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_payment_purposes(db: Session):
    return db.query(PaymentPurpose).order_by(PaymentPurpose.created_at.desc()).all()


def _ensure_default_plans(db: Session) -> None:
    # Keep existing installs working by seeding defaults once.
    if db.query(SubscriptionPlan).count() > 0:
        return

    db.add_all(
        [
            SubscriptionPlan(
                id=row["id"],
                name=row["name"],
                amount=int(row["amount"]),
                currency=row.get("currency") or "NGN",
                max_doors=int(row.get("maxDoors") or 1),
                max_qr_codes=int(row.get("maxQrCodes") or 1),
                active=True,
            )
            for row in DEFAULT_PLAN_CATALOG
        ]
    )
    db.commit()


def list_subscription_plans(db: Session, include_inactive: bool = False):
    _ensure_default_plans(db)
    q = db.query(SubscriptionPlan).order_by(SubscriptionPlan.amount.asc(), SubscriptionPlan.id.asc())
    if not include_inactive:
        q = q.filter(SubscriptionPlan.active == True)  # noqa: E712
    rows = q.all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "amount": int(row.amount or 0),
            "currency": row.currency or "NGN",
            "maxDoors": int(row.max_doors or 0),
            "maxQrCodes": int(row.max_qr_codes or 0),
            "active": bool(row.active),
        }
        for row in rows
    ]


def get_plan_or_raise(db: Session, plan_id: str, include_inactive: bool = False):
    _ensure_default_plans(db)
    q = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id)
    if not include_inactive:
        q = q.filter(SubscriptionPlan.active == True)  # noqa: E712
    row = q.first()
    if row:
        return {
            "id": row.id,
            "name": row.name,
            "amount": int(row.amount or 0),
            "currency": row.currency or "NGN",
            "maxDoors": int(row.max_doors or 0),
            "maxQrCodes": int(row.max_qr_codes or 0),
            "active": bool(row.active),
        }
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


def get_effective_subscription(db: Session, user_id: str):
    row = get_user_subscription(db, user_id)
    now = datetime.utcnow()
    if row and row.status == "active":
        if row.plan != "free":
            expiry_at = row.ends_at or (
                (row.starts_at + timedelta(days=30)) if row.starts_at else None
            )
            if expiry_at and now > expiry_at:
                row.status = "expired"
                row.ends_at = row.ends_at or expiry_at
                db.commit()
            else:
                try:
                    plan_meta = get_plan_or_raise(db, row.plan) if row.plan else get_plan_or_raise(db, "free")
                except AppException:
                    # Fail closed to free plan limits when an unexpected plan id exists.
                    plan_meta = get_plan_or_raise(db, "free")
                    row.plan = "free"
                return {
                    "id": row.id,
                    "plan": row.plan,
                    "status": row.status,
                    "startsAt": row.starts_at.isoformat() if row.starts_at else None,
                    "endsAt": row.ends_at.isoformat() if row.ends_at else None,
                    "limits": {
                        "maxDoors": plan_meta["maxDoors"],
                        "maxQrCodes": plan_meta["maxQrCodes"],
                    },
                }
        try:
            plan_meta = get_plan_or_raise(db, row.plan) if row.plan else get_plan_or_raise(db, "free")
        except AppException:
            # Fail closed to free plan limits when an unexpected plan id exists.
            plan_meta = get_plan_or_raise(db, "free")
            row.plan = "free"
        return {
            "id": row.id,
            "plan": row.plan,
            "status": row.status,
            "startsAt": row.starts_at.isoformat() if row.starts_at else None,
            "endsAt": row.ends_at.isoformat() if row.ends_at else None,
            "limits": {
                "maxDoors": plan_meta["maxDoors"],
                "maxQrCodes": plan_meta["maxQrCodes"],
            },
        }

    free_plan = get_plan_or_raise(db, "free")
    return {
        "id": None,
        "plan": "free",
        "status": "active",
        "startsAt": None,
        "endsAt": None,
        "limits": {
            "maxDoors": free_plan["maxDoors"],
            "maxQrCodes": free_plan["maxQrCodes"],
        },
    }


def is_paid_subscription_expired(db: Session, user_id: str) -> bool:
    row = get_user_subscription(db, user_id)
    if not row or row.plan == "free":
        return False
    if row.status != "active":
        return True
    expiry_at = row.ends_at or ((row.starts_at + timedelta(days=30)) if row.starts_at else None)
    return bool(expiry_at and datetime.utcnow() > expiry_at)


def initialize_paystack_transaction(user_id: str, email: str, plan_id: str, callback_url: str | None):
    raise AppException("Internal error: use initialize_paystack_transaction_db", status_code=500)


def initialize_paystack_transaction_db(db: Session, user_id: str, email: str, plan_id: str, callback_url: str | None):
    plan = get_plan_or_raise(db, plan_id)
    if plan["amount"] <= 0:
        raise AppException("Free plan does not require Paystack checkout", status_code=400)
    if not settings.PAYSTACK_SECRET_KEY:
        raise AppException("Paystack is not configured", status_code=500)
    if settings.PAYSTACK_SECRET_KEY.startswith("sk_live") and (
        "localhost" in settings.FRONTEND_BASE_URL or "127.0.0.1" in settings.FRONTEND_BASE_URL
    ):
        raise AppException(
            "Live Paystack cannot be initialized with localhost frontend. Use a public HTTPS domain in FRONTEND_BASE_URL or use test keys for local development.",
            status_code=400,
        )

    reference = f"qring-{uuid.uuid4().hex[:18]}"
    payload = {
        "email": email,
        "amount": int(plan["amount"] * 100),
        "currency": "NGN",
        "reference": reference,
        "metadata": {
            "user_id": user_id,
            "plan": plan_id,
            "source": "qring-billing",
        },
    }

    resolved_callback = callback_url or f"{settings.FRONTEND_BASE_URL}/billing/callback"
    callback_is_public_https = (
        isinstance(resolved_callback, str)
        and resolved_callback.startswith("https://")
        and "localhost" not in resolved_callback
        and "127.0.0.1" not in resolved_callback
    )
    if callback_is_public_https:
        payload["callback_url"] = resolved_callback
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        "https://api.paystack.co/transaction/initialize",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        if '"code":"1010"' in detail or '"code":1010' in detail:
            raise AppException(
                "Paystack blocked this initialization (1010). Check IP whitelist, live-mode domain/callback, and use public HTTPS URLs.",
                status_code=502,
            )
        raise AppException(f"Paystack initialize failed: {detail}", status_code=502)
    except Exception:
        raise AppException("Paystack initialize failed", status_code=502)

    if not data.get("status") or not data.get("data", {}).get("authorization_url"):
        raise AppException("Unable to initialize payment", status_code=502)
    return data["data"]


def verify_paystack_and_activate(db: Session, reference: str, user_id: str):
    if not settings.PAYSTACK_SECRET_KEY:
        raise AppException("Paystack is not configured", status_code=500)

    req = request.Request(
        f"https://api.paystack.co/transaction/verify/{reference}",
        method="GET",
        headers={
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise AppException(f"Paystack verify failed: {detail}", status_code=502)
    except Exception:
        raise AppException("Paystack verify failed", status_code=502)

    if not data.get("status"):
        raise AppException("Unable to verify payment", status_code=400)

    payment = data.get("data", {})
    if payment.get("status") != "success":
        raise AppException("Payment not successful", status_code=400)

    metadata = payment.get("metadata") or {}
    payment_user_id = metadata.get("user_id")
    plan_id = metadata.get("plan")
    if payment_user_id != user_id:
        raise AppException("Payment reference is not linked to this user", status_code=403)
    plan = get_plan_or_raise(db, plan_id)

    row = activate_subscription(db, user_id=user_id, plan=plan["id"])
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
    if not settings.PAYSTACK_SECRET_KEY:
        raise AppException("Paystack is not configured", status_code=500)
    if not signature:
        raise AppException("Missing Paystack signature", status_code=400)

    computed = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
        msg=raw_body,
        digestmod=sha512,
    ).hexdigest()

    if not hmac.compare_digest(computed, signature):
        raise AppException("Invalid Paystack signature", status_code=401)

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise AppException("Invalid webhook payload", status_code=400)

    if event.get("event") != "charge.success":
        return {"status": "ignored"}

    data = event.get("data") or {}
    metadata = data.get("metadata") or {}
    user_id = metadata.get("user_id")
    plan_id = metadata.get("plan")
    payment_status = data.get("status")

    if payment_status != "success" or not user_id or not plan_id:
        return {"status": "ignored"}

    get_plan_or_raise(db, plan_id)
    row = Subscription(
        user_id=user_id,
        plan=plan_id,
        status="active",
        starts_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    return {"status": "processed", "plan": plan_id}
