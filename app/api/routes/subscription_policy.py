from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.services.payment_service import get_effective_subscription

router = APIRouter()


@router.get("/subscription/summary")
def subscription_summary(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": get_effective_subscription(db, user.id, user_role=user.role.value)}


@router.post("/subscription/recompute")
def subscription_recompute_stub(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {
        "data": {
            "recomputed": True,
            "summary": get_effective_subscription(db, user.id, user_role=user.role.value),
        }
    }
