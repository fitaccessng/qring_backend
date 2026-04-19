from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.api.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.services.estate_alert_service import delete_estate_alert, initialize_alert_payment

router = APIRouter()


class AlertPayPayload(BaseModel):
    paymentMethod: str = "paystack"
    reference: Optional[str] = None
    callbackUrl: Optional[str] = None


@router.post("/{alert_id}/pay")
def alert_pay(
    alert_id: str,
    payload: AlertPayPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = initialize_alert_payment(
        db=db,
        alert_id=alert_id,
        homeowner_id=user.id,
        payment_method=payload.paymentMethod,
        reference=payload.reference,
        callback_url=payload.callbackUrl,
    )
    return {"data": data}


@router.delete("/{alert_id}")
def alert_delete(
    alert_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = delete_estate_alert(db=db, alert_id=alert_id, estate_admin_id=user.id)
    return {"data": data}
