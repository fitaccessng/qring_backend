from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.core.exceptions import AppException
from app.services.notification_service import (
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)

router = APIRouter()


class PushSubscriptionCreate(BaseModel):
    endpoint: str
    keys: dict


@router.get("/")
def notifications(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": list_notifications(db, user.id)}


@router.post("/push-subscriptions")
def add_push_subscription(
    payload: PushSubscriptionCreate,
    user: User = Depends(get_current_user),
):
    # Persist to DB/Redis in production. This is a stable API contract for PWA integration.
    return {"data": {"userId": user.id, "endpoint": payload.endpoint, "status": "registered"}}


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
