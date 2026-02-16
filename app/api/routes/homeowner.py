from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.services.homeowner_settings_service import (
    get_homeowner_settings_payload,
    update_homeowner_settings,
)
from app.services.homeowner_service import (
    create_homeowner_session_message,
    create_homeowner_door,
    delete_homeowner_session_message,
    get_homeowner_context,
    get_homeowner_doors_data,
    generate_homeowner_door_qr,
    list_homeowner_session_messages,
    list_homeowner_message_threads,
    list_homeowner_visits,
)
from app.services.session_service import mark_session_status
from app.core.exceptions import AppException
from app.socket.server import sio
from app.core.config import get_settings

router = APIRouter()
settings = get_settings()


class DoorQrCreate(BaseModel):
    mode: str = "direct"
    plan: str = "single"


class HomeownerDoorCreate(BaseModel):
    name: str
    generateQr: bool = True
    mode: str = "direct"
    plan: str = "single"


class HomeownerSettingsUpdate(BaseModel):
    pushAlerts: bool
    soundAlerts: bool
    autoRejectUnknownVisitors: bool


class VisitDecisionPayload(BaseModel):
    action: str


class HomeownerMessagePayload(BaseModel):
    text: str


@router.get("/visits")
def homeowner_visits(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": list_homeowner_visits(db, homeowner_id=user.id)}


@router.get("/context")
def homeowner_context(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": get_homeowner_context(db, homeowner_id=user.id)}


@router.get("/messages")
def homeowner_messages(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": list_homeowner_message_threads(db, homeowner_id=user.id)}


@router.get("/messages/{session_id}")
def homeowner_session_messages(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    rows = list_homeowner_session_messages(db, homeowner_id=user.id, session_id=session_id)
    return {"data": rows}


@router.post("/messages/{session_id}")
async def homeowner_send_message(
    session_id: str,
    payload: HomeownerMessagePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = create_homeowner_session_message(
        db,
        homeowner_id=user.id,
        session_id=session_id,
        text=payload.text,
    )
    if not data:
        raise AppException("Unable to send message", status_code=400)
    await sio.emit(
        "chat.message",
        {
            **data,
            "displayName": user.full_name or "Homeowner",
        },
        room=f"session:{session_id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": data}


@router.delete("/messages/{session_id}/{message_id}")
def homeowner_delete_message(
    session_id: str,
    message_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    deleted = delete_homeowner_session_message(
        db,
        homeowner_id=user.id,
        session_id=session_id,
        message_id=message_id,
    )
    if not deleted:
        raise AppException("Message not found", status_code=404)
    return {"data": {"id": message_id, "deleted": True}}


@router.get("/doors")
def homeowner_doors(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": get_homeowner_doors_data(db, homeowner_id=user.id)}


@router.post("/doors")
def homeowner_create_door(
    payload: HomeownerDoorCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = create_homeowner_door(
        db=db,
        homeowner_id=user.id,
        name=payload.name,
        generate_qr=payload.generateQr,
        mode=payload.mode,
        qr_plan=payload.plan,
    )
    return {"data": data}


@router.post("/doors/{door_id}/qr")
def homeowner_generate_door_qr(
    door_id: str,
    payload: DoorQrCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = generate_homeowner_door_qr(
        db=db,
        homeowner_id=user.id,
        door_id=door_id,
        mode=payload.mode,
        plan=payload.plan,
    )
    return {"data": data}


@router.get("/settings")
def homeowner_settings(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": get_homeowner_settings_payload(db, user.id)}


@router.put("/settings")
def homeowner_update_settings(
    payload: HomeownerSettingsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    updated = update_homeowner_settings(
        db=db,
        user_id=user.id,
        push_alerts=payload.pushAlerts,
        sound_alerts=payload.soundAlerts,
        auto_reject_unknown_visitors=payload.autoRejectUnknownVisitors,
    )
    return {"data": updated}


@router.post("/visits/{session_id}/decision")
def homeowner_visit_decision(
    session_id: str,
    payload: VisitDecisionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    action = (payload.action or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise AppException("Action must be approve or reject", status_code=400)

    from app.db.models import VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id, VisitorSession.homeowner_id == user.id).first()
    if not session:
        raise AppException("Visit not found", status_code=404)

    updated = mark_session_status(db, session_id=session_id, status="approved" if action == "approve" else "rejected")
    if not updated:
        raise AppException("Visit not found", status_code=404)
    return {"data": {"id": updated.id, "status": updated.status}}


@router.post("/visits/{session_id}/end")
async def homeowner_end_visit(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    from app.db.models import VisitorSession

    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.id == session_id, VisitorSession.homeowner_id == user.id)
        .first()
    )
    if not session:
        raise AppException("Visit not found", status_code=404)

    updated = mark_session_status(db, session_id=session_id, status="closed")
    if not updated:
        raise AppException("Visit not found", status_code=404)

    await sio.emit(
        "session.control",
        {"sessionId": session_id, "action": "end"},
        room=f"session:{session_id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": {"id": updated.id, "status": updated.status}}

