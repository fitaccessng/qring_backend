import json
import hmac
import uuid
from datetime import datetime, timedelta
from hashlib import sha512
from urllib.parse import urlparse
from urllib import error, request

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import Notification, PaymentPurpose, ReferralReward, Subscription, SubscriptionPlan, User

settings = get_settings()
REFERRAL_REWARD_AMOUNT = 2000

DEFAULT_PLAN_CATALOG = [
    {
        "id": "estate_starter",
        "name": "Starter Estate",
        "amount": 0,
        "currency": "NGN",
        "maxDoors": 3,
        "maxQrCodes": 3,
        "active": True,
        "audience": "estate",
        "trialDays": 60,
        "selfServe": True,
        "description": "Trial only - 60 days",
    },
    {
        "id": "estate_basic",
        "name": "Estate Basic",
        "amount": 8000,
        "currency": "NGN",
        "maxDoors": 10,
        "maxQrCodes": 10,
        "active": True,
        "audience": "estate",
        "selfServe": True,
    },
    {
        "id": "estate_growth",
        "name": "Estate Growth",
        "amount": 18000,
        "currency": "NGN",
        "maxDoors": 25,
        "maxQrCodes": 25,
        "active": True,
        "audience": "estate",
        "selfServe": True,
    },
    {
        "id": "estate_pro",
        "name": "Estate Pro",
        "amount": 35000,
        "currency": "NGN",
        "maxDoors": 60,
        "maxQrCodes": 60,
        "active": True,
        "audience": "estate",
        "selfServe": True,
    },
    {
        "id": "estate_enterprise",
        "name": "Enterprise Estate",
        "amount": 0,
        "currency": "NGN",
        "maxDoors": 0,
        "maxQrCodes": 0,
        "active": True,
        "audience": "estate",
        "selfServe": False,
        "description": "Custom annual contract",
    },
    {
        "id": "free",
        "name": "Free",
        "amount": 0,
        "currency": "NGN",
        "maxDoors": 1,
        "maxQrCodes": 1,
        "active": True,
        "audience": "homeowner",
        "selfServe": True,
    },
    {
        "id": "home_pro",
        "name": "Home Pro",
        "amount": 2500,
        "currency": "NGN",
        "maxDoors": 1,
        "maxQrCodes": 5,
        "active": True,
        "audience": "homeowner",
        "selfServe": True,
    },
    {
        "id": "home_premium",
        "name": "Home Premium",
        "amount": 4500,
        "currency": "NGN",
        "maxDoors": 5,
        "maxQrCodes": 20,
        "active": True,
        "audience": "homeowner",
        "selfServe": True,
    },
    # Legacy plans retained for backwards compatibility with existing subscriptions.
    {"id": "doors_20", "name": "Legacy Basic Plan", "amount": 12000, "currency": "NGN", "maxDoors": 10, "maxQrCodes": 10, "active": True, "audience": "legacy", "selfServe": False, "hidden": True},
    {"id": "doors_40", "name": "Legacy Standard Plan", "amount": 25000, "currency": "NGN", "maxDoors": 22, "maxQrCodes": 22, "active": True, "audience": "legacy", "selfServe": False, "hidden": True},
    {"id": "doors_80", "name": "Legacy Pro Estate Plan", "amount": 50000, "currency": "NGN", "maxDoors": 46, "maxQrCodes": 46, "active": True, "audience": "legacy", "selfServe": False, "hidden": True},
    {"id": "doors_100", "name": "Legacy Premium Estate Plan", "amount": 100000, "currency": "NGN", "maxDoors": 100, "maxQrCodes": 100, "active": True, "audience": "legacy", "selfServe": False, "hidden": True},
]


def _normalize_url(value: str | None) -> str:
    return (value or "").strip().rstrip("/")


def _is_public_https_url(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host not in {"", "localhost", "127.0.0.1"}


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
    return code, str(message).strip()


def create_payment_purpose(db: Session, name: str, description: str, account_info: str):
    purpose = PaymentPurpose(name=name, description=description, account_info=account_info)
    db.add(purpose)
    db.commit()
    db.refresh(purpose)
    return purpose


def activate_subscription(db: Session, user_id: str, plan: str):
    plan_meta = get_plan_or_raise(db, plan, include_inactive=True)
    row = Subscription(
        user_id=user_id,
        plan=plan,
        status="active",
        starts_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
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
        plan.max_doors = int(row.get("maxDoors") or 1)
        plan.max_qr_codes = int(row.get("maxQrCodes") or 1)
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
    catalog_by_id = {row["id"]: row for row in DEFAULT_PLAN_CATALOG}
    return [
        {
            "id": row.id,
            "name": row.name,
            "amount": int(row.amount or 0),
            "currency": row.currency or "NGN",
            "maxDoors": int(row.max_doors or 0),
            "maxQrCodes": int(row.max_qr_codes or 0),
            "active": bool(row.active),
            "audience": (catalog_by_id.get(row.id) or {}).get("audience", "homeowner"),
            "trialDays": int((catalog_by_id.get(row.id) or {}).get("trialDays", 0) or 0),
            "selfServe": bool((catalog_by_id.get(row.id) or {}).get("selfServe", True)),
            "hidden": bool((catalog_by_id.get(row.id) or {}).get("hidden", False)),
            "description": (catalog_by_id.get(row.id) or {}).get("description", ""),
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
        catalog_row = next((item for item in DEFAULT_PLAN_CATALOG if item["id"] == row.id), {})
        return {
            "id": row.id,
            "name": row.name,
            "amount": int(row.amount or 0),
            "currency": row.currency or "NGN",
            "maxDoors": int(row.max_doors or 0),
            "maxQrCodes": int(row.max_qr_codes or 0),
            "active": bool(row.active),
            "audience": catalog_row.get("audience", "homeowner"),
            "trialDays": int(catalog_row.get("trialDays", 0) or 0),
            "selfServe": bool(catalog_row.get("selfServe", True)),
            "hidden": bool(catalog_row.get("hidden", False)),
            "description": catalog_row.get("description", ""),
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
        try:
            plan_meta = get_plan_or_raise(db, row.plan) if row.plan else get_plan_or_raise(db, "free")
        except AppException:
            plan_meta = get_plan_or_raise(db, "free")
            row.plan = "free"

        trial_days = int(plan_meta.get("trialDays") or 0)
        if row.plan != "free":
            expiry_days = trial_days if trial_days > 0 else 30
            expiry_at = row.ends_at or ((row.starts_at + timedelta(days=expiry_days)) if row.starts_at else None)
            if expiry_at and now > expiry_at:
                row.status = "expired"
                row.ends_at = row.ends_at or expiry_at
                db.commit()
            else:
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
    cycle_multiplier = 12 if cycle == "yearly" else 1
    if not settings.PAYSTACK_SECRET_KEY:
        raise AppException("Paystack is not configured", status_code=500)
    frontend_base_url = _normalize_url(settings.FRONTEND_BASE_URL)
    if settings.PAYSTACK_SECRET_KEY.startswith("sk_live") and (
        "localhost" in frontend_base_url or "127.0.0.1" in frontend_base_url
    ):
        raise AppException(
            "Live Paystack cannot be initialized with localhost frontend. Use a public HTTPS domain in FRONTEND_BASE_URL or use test keys for local development.",
            status_code=400,
        )

    reference = f"qring-{uuid.uuid4().hex[:18]}"
    payload = {
        "email": email,
        "amount": int(plan["amount"] * cycle_multiplier * 100),
        "currency": (plan.get("currency") or "NGN").upper(),
        "reference": reference,
        "metadata": {
            "user_id": user_id,
            "plan": plan_id,
            "billing_cycle": cycle,
            "source": "qring-billing",
        },
    }

    normalized_callback = _normalize_url(callback_url)
    resolved_callback = normalized_callback or f"{frontend_base_url}/billing/callback"
    callback_is_public_https = _is_public_https_url(resolved_callback)
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
        error_code, error_message = _extract_paystack_error(detail)
        if error_code == "1010":
            raise AppException(
                f"Paystack blocked initialization (1010: {error_message or 'operation blocked'}). "
                f"Check Paystack live-mode restrictions: callback/domain allowlist and server IP allowlist. "
                f"frontendBaseUrl={frontend_base_url}, callback={resolved_callback if callback_is_public_https else '<omitted>'}",
                status_code=502,
            )
        raise AppException(f"Paystack initialize failed: {error_message or detail}", status_code=502)
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
    if payment_status != "success" or not user_id or not plan_id:
        return {"status": "ignored"}

    activate_subscription(db=db, user_id=user_id, plan=plan_id)
    return {"status": "processed", "plan": plan_id}
