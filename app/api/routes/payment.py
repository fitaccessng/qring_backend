from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_roles
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import User
from app.db.session import get_db
from app.services.payment_service import (
    activate_subscription,
    create_payment_purpose,
    get_effective_subscription,
    get_referral_summary,
    handle_paystack_webhook,
    initialize_paystack_transaction_db,
    list_subscription_plans,
    list_payment_purposes,
    get_plan_or_raise,
    verify_paystack_and_activate,
)

router = APIRouter()
settings = get_settings()


class PaymentPurposeCreate(BaseModel):
    name: str
    description: str = ""
    accountInfo: str = ""


class SubscriptionActivate(BaseModel):
    userId: str
    plan: str


class SubscriptionRequest(BaseModel):
    plan: str


class PaystackInitializePayload(BaseModel):
    plan: str
    callbackUrl: str | None = None


@router.post("/purpose")
def payment_create_purpose(
    payload: PaymentPurposeCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    purpose = create_payment_purpose(db, payload.name, payload.description, payload.accountInfo)
    return {"data": {"id": purpose.id, "name": purpose.name}}


@router.post("/subscription/activate")
def payment_activate_subscription(
    payload: SubscriptionActivate,
    db: Session = Depends(get_db),
    _: User = Depends(require_roles("admin")),
):
    get_plan_or_raise(db, payload.plan, include_inactive=True)
    sub = activate_subscription(db, payload.userId, payload.plan)
    return {"data": {"id": sub.id, "plan": sub.plan, "status": sub.status}}


@router.get("/purposes")
def payment_list_purposes(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    rows = list_payment_purposes(db)
    return {
        "data": [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "accountInfo": row.account_info,
            }
            for row in rows
        ]
    }


@router.get("/subscription/me")
def payment_subscription_me(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": get_effective_subscription(db, user.id)}


@router.get("/referral/me")
def payment_referral_me(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": get_referral_summary(db, user.id)}


@router.get("/plans")
def payment_plans(
    db: Session = Depends(get_db),
):
    return {"data": list_subscription_plans(db)}


@router.post("/paystack/initialize")
def payment_paystack_initialize(
    payload: PaystackInitializePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "estate")),
):
    data = initialize_paystack_transaction_db(
        db=db,
        user_id=user.id,
        email=user.email,
        plan_id=payload.plan,
        callback_url=payload.callbackUrl,
    )
    return {"data": data}


@router.get("/paystack/verify/{reference}")
def payment_paystack_verify(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "estate")),
):
    data = verify_paystack_and_activate(db=db, reference=reference, user_id=user.id)
    return {"data": data}


@router.post("/subscription/request")
def payment_request_subscription(
    payload: SubscriptionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "estate")),
):
    if payload.plan != "free":
        raise AppException("Use Paystack checkout for paid plans", status_code=400)
    sub = activate_subscription(db, user_id=user.id, plan=payload.plan)
    return {"data": {"id": sub.id, "plan": sub.plan, "status": sub.status}}


@router.post("/paystack/webhook")
async def payment_paystack_webhook(
    request: Request,
    x_paystack_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    raw = await request.body()
    data = handle_paystack_webhook(
        db=db,
        raw_body=raw,
        signature=x_paystack_signature,
    )
    return {"data": data}
