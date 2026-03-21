from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.core.exceptions import AppException
from app.services.notification_service import (
    clear_all_notifications,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)
from app.services.provider_integrations import upsert_push_subscription

router = APIRouter()


class PushSubscriptionCreate(BaseModel):
    provider: str = "fcm"
    endpoint: str
    keys: dict
    token: Optional[str] = None


@router.get("/")
def notifications(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": list_notifications(db, user.id)}


@router.post("/push-subscriptions")
def add_push_subscription(
    payload: PushSubscriptionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        data = upsert_push_subscription(
            db,
            user_id=user.id,
            provider=payload.provider,
            endpoint=payload.endpoint,
            token=payload.token or payload.keys.get("token"),
            keys=payload.keys,
        )
    except ValueError as exc:
        raise AppException(str(exc), status_code=400) from exc
    return {"data": {"userId": user.id, **data, "status": "registered"}}


@router.post("/{notification_id}/read")
def read_notification(
    notification_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = mark_notification_read(db, user.id, notification_id)
    if not data:
        raise AppException("Notification not found", status_code=404)
    return {"data": data}


@router.post("/read-all")
def read_all_notifications(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    count = mark_all_notifications_read(db, user.id)
    return {"data": {"updated": count}}


@router.delete("/clear-all")
def clear_notifications(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    count = clear_all_notifications(db, user.id)
    return {"data": {"deleted": count}}
