from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.core.exceptions import AppException
from app.db.models import User
from app.db.session import get_db
from app.services.estate_alert_service import initialize_alert_payment

router = APIRouter()


class AlertPayPayload(BaseModel):
    paymentMethod: str = "paystack"
    callbackUrl: str | None = None


@router.post("/{alert_id}/pay")
def alert_pay(
    alert_id: str,
    payload: AlertPayPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    if (payload.paymentMethod or "paystack").strip().lower() != "paystack":
        raise AppException("Only paystack payment method is supported", status_code=400)
    data = initialize_alert_payment(
        db=db,
        alert_id=alert_id,
        homeowner_id=user.id,
        callback_url=payload.callbackUrl,
    )
    return {"data": data}
