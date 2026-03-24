from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.services.safety_service import (
    cancel_emergency_alert,
    get_safety_dashboard,
    get_watchlist,
    list_emergency_alerts,
    report_visitor,
    trigger_emergency_alert,
    update_emergency_alert_status,
)

router = APIRouter()


class EmergencyAlertCreatePayload(BaseModel):
    alertType: str
    triggerMode: str = "hold"
    silentTrigger: bool = False
    cancelWindowSeconds: int = Field(default=8, ge=5, le=10)
    offlineQueued: bool = False
    notes: Optional[str] = None
    location: Optional[dict] = None


class EmergencyAlertCancelPayload(BaseModel):
    reason: Optional[str] = None


class EmergencyAlertActionPayload(BaseModel):
    action: str
    notes: Optional[str] = None


class VisitorReportPayload(BaseModel):
    visitorSessionId: Optional[str] = None
    reportedName: Optional[str] = None
    reportedPhone: Optional[str] = None
    reason: str
    notes: Optional[str] = None
    severity: str = "medium"


@router.get("/dashboard")
def safety_dashboard(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": get_safety_dashboard(db, actor=user)}


@router.get("/alerts")
def safety_alerts(
    limit: int = 40,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": list_emergency_alerts(db, actor=user, limit=limit)}


@router.post("/alerts")
def safety_create_alert(
    payload: EmergencyAlertCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {
        "data": trigger_emergency_alert(
            db,
            user=user,
            alert_type=payload.alertType,
            trigger_mode=payload.triggerMode,
            silent_trigger=payload.silentTrigger,
            cancel_window_seconds=payload.cancelWindowSeconds,
            location=payload.location or {},
            offline_queued=payload.offlineQueued,
            notes=payload.notes,
        )
    }


@router.post("/alerts/{alert_id}/cancel")
def safety_cancel_alert(
    alert_id: str,
    payload: EmergencyAlertCancelPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": cancel_emergency_alert(db, alert_id=alert_id, user=user, reason=payload.reason)}


@router.post("/alerts/{alert_id}/action")
def safety_alert_action(
    alert_id: str,
    payload: EmergencyAlertActionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security", "estate", "admin")),
):
    return {
        "data": update_emergency_alert_status(
            db,
            alert_id=alert_id,
            actor=user,
            action=payload.action,
            notes=payload.notes,
        )
    }


@router.get("/watchlist")
def safety_watchlist(
    limit: int = 30,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": get_watchlist(db, actor=user, limit=limit)}


@router.post("/visitor-reports")
def safety_visitor_report(
    payload: VisitorReportPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate")),
):
    return {
        "data": report_visitor(
            db,
            actor=user,
            visitor_session_id=payload.visitorSessionId,
            reported_name=payload.reportedName,
            reported_phone=payload.reportedPhone,
            reason=payload.reason,
            notes=payload.notes,
            severity=payload.severity,
        )
    }
