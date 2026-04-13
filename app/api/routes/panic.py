from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.services.safety_service import acknowledge_panic_event, list_active_panic_events, resolve_panic_event, trigger_panic_event

router = APIRouter()


class PanicTriggerPayload(BaseModel):
    userId: Optional[str] = None
    triggerMode: str = "hold"
    location: Optional[dict] = None
    offlineQueued: bool = False


class PanicAcknowledgePayload(BaseModel):
    panicId: str


class PanicResolvePayload(BaseModel):
    panicId: str


@router.post("/trigger")
def panic_trigger(
    payload: PanicTriggerPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "admin")),
):
    return {
        "data": trigger_panic_event(
            db,
            actor=user,
            user_id=payload.userId,
            trigger_mode=payload.triggerMode,
            location=payload.location or {},
            offline_queued=payload.offlineQueued,
        )
    }


@router.post("/acknowledge")
def panic_acknowledge(
    payload: PanicAcknowledgePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": acknowledge_panic_event(db, panic_id=payload.panicId, actor=user)}


@router.post("/resolve")
def panic_resolve(
    payload: PanicResolvePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": resolve_panic_event(db, panic_id=payload.panicId, actor=user)}


@router.get("/active")
def panic_active(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": list_active_panic_events(db, actor=user)}
