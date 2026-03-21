from __future__ import annotations

import base64
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.api.deps import require_roles
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import Door, Home, User
from app.db.session import get_db
from app.services.advanced_service import create_snapshot_audit
from app.services.notification_service import create_notification
from app.services.session_service import create_visitor_session
from app.services.security_service import (
    create_security_session_message,
    delete_security_session_message,
    get_security_dashboard,
    list_security_message_threads,
    list_security_session_messages,
    serialize_security_session,
    update_security_session_status,
)
from app.services.access_pass_service import validate_access_pass
from app.socket.server import sio

router = APIRouter()
settings = get_settings()


class SecurityActionPayload(BaseModel):
    action: str


class SecurityMessagePayload(BaseModel):
    text: str
    clientId: Optional[str] = None


class AccessPassValidationPayload(BaseModel):
    codeValue: str


class SecurityRegisterVisitorPayload(BaseModel):
    requestId: Optional[str] = None
    name: Optional[str] = None
    purpose: Optional[str] = None
    visitorType: Optional[str] = None
    phoneNumber: Optional[str] = None
    doorId: str
    snapshotBase64: str
    snapshotMime: Optional[str] = None


@router.get("/dashboard")
def security_dashboard(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    return {"data": get_security_dashboard(db, security_user_id=user.id)}


@router.get("/messages")
def security_messages(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    return {"data": list_security_message_threads(db, security_user_id=user.id)}


@router.get("/messages/{session_id}")
def security_session_messages(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    return {"data": list_security_session_messages(db, security_user_id=user.id, session_id=session_id)}


@router.get("/door-options")
def security_door_options(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    if not user.estate_id:
        return {"data": []}

    query = (
        db.query(Door, Home, User)
        .join(Home, Home.id == Door.home_id)
        .join(User, User.id == Home.homeowner_id)
        .filter(Home.estate_id == user.estate_id)
        .order_by(Home.name.asc(), Door.name.asc())
    )
    if user.gate_id:
        query = query.filter((Door.gate_label == user.gate_id) | (Door.gate_label.is_(None)))

    rows = query.all()
    return {
        "data": [
            {
                "id": door.id,
                "name": door.name,
                "homeId": home.id,
                "homeName": home.name,
                "homeownerId": homeowner.id,
                "homeownerName": homeowner.full_name,
                "gateLabel": door.gate_label or "Main Gate",
            }
            for door, home, homeowner in rows
        ]
    }


@router.post("/requests/register")
async def security_register_request(
    payload: SecurityRegisterVisitorPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    door = (
        db.query(Door, Home)
        .join(Home, Home.id == Door.home_id)
        .filter(Door.id == payload.doorId)
        .first()
    )
    if not door:
        raise AppException("Selected door was not found.", status_code=404)
    door_row, home = door
    if user.estate_id and home.estate_id != user.estate_id:
        raise AppException("You cannot register a visitor outside your estate.", status_code=403)
    if user.gate_id and door_row.gate_label and door_row.gate_label != user.gate_id:
        raise AppException("This door is assigned to a different gate.", status_code=403)

    snapshot_b64 = (payload.snapshotBase64 or "").strip()
    if not snapshot_b64:
        raise AppException("A live visitor photo is required.", status_code=400)
    try:
        media_bytes = base64.b64decode(snapshot_b64, validate=True)
    except Exception as exc:
        raise AppException("The captured visitor photo could not be processed.", status_code=400) from exc
    if not media_bytes:
        raise AppException("A live visitor photo is required.", status_code=400)
    if len(media_bytes) > 2 * 1024 * 1024:
        raise AppException("Snapshot is too large. Please retake the photo.", status_code=400)

    session = create_visitor_session(
        db=db,
        qr_id="manual-security-entry",
        qr_home_id=home.id,
        doors=[door_row.id],
        mode="direct",
        requested_door=door_row.id,
        visitor_label=(payload.name or "").strip() or "Visitor",
        request_id=(payload.requestId or "").strip() or None,
        visitor_phone=(payload.phoneNumber or "").strip() or None,
        purpose=(payload.purpose or "").strip() or None,
        visitor_type=(payload.visitorType or "guest").strip().lower() or "guest",
        request_source="gateman_assisted",
        creator_role="security",
    )

    mime = (payload.snapshotMime or "image/jpeg").strip().lower()
    ext = ".jpg"
    if "png" in mime:
        ext = ".png"
    elif "webp" in mime:
        ext = ".webp"
    snapshot_audit = create_snapshot_audit(
        db,
        homeowner_id=session.homeowner_id,
        media_bytes=media_bytes,
        filename_hint=f"security-register-visitor{ext}",
        media_type="photo",
        visitor_session_id=session.id,
        source="security_register_visitor",
    )
    if snapshot_audit and isinstance(snapshot_audit, dict):
        session.photo_url = str(snapshot_audit.get("fileUrl") or snapshot_audit.get("url") or "").strip() or None
        db.commit()
        db.refresh(session)

    updated = update_security_session_status(
        db,
        session_id=session.id,
        actor=user,
        action="forward",
    )

    create_notification(
        db=db,
        user_id=updated.homeowner_id,
        kind="visitor.request",
        payload={
            "sessionId": updated.id,
            "doorId": updated.door_id,
            "visitorName": updated.visitor_label or "Visitor",
            "phoneNumber": updated.visitor_phone or "",
            "purpose": updated.purpose or "",
            "photoUrl": updated.photo_url,
            "snapshotAuditId": snapshot_audit.get("id") if isinstance(snapshot_audit, dict) else None,
            "requestSource": "gateman_assisted",
            "creatorRole": "security",
            "message": f"Gate security registered {updated.visitor_label or 'a visitor'} for your approval.",
        },
    )

    serialized = serialize_security_session(db, updated)
    await sio.emit(
        "visitor_forwarded",
        {"data": serialized, "action": "forward", "actorRole": user.role.value},
        namespace=settings.DASHBOARD_NAMESPACE,
    )
    await sio.emit(
        "session.status",
        {
            "sessionId": updated.id,
            "status": updated.status,
            "gateStatus": updated.gate_status,
            "communicationStatus": updated.communication_status,
            "homeownerId": updated.homeowner_id,
        },
        room=f"session:{updated.id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    await sio.emit(
        "incoming-call",
        {
            "sessionId": updated.id,
            "callSessionId": "",
            "homeownerId": updated.homeowner_id,
            "visitorId": updated.id,
            "visitorName": updated.visitor_label or "Visitor",
            "doorId": updated.door_id,
            "hasVideo": False,
            "state": "ringing",
            "message": f"Security registered {updated.visitor_label or 'a visitor'} at your gate.",
        },
        room=f"user:{updated.homeowner_id}",
        namespace=settings.DASHBOARD_NAMESPACE,
    )
    return {"data": serialized}


@router.post("/messages/{session_id}")
async def security_send_message(
    session_id: str,
    payload: SecurityMessagePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    data = create_security_session_message(
        db,
        security_user_id=user.id,
        session_id=session_id,
        text=payload.text,
    )
    if not data:
        raise AppException("Unable to send message", status_code=400)
    await sio.emit(
        "chat.message",
        {
            **data,
            "clientId": payload.clientId,
            "displayName": user.full_name or "Security",
        },
        room=f"session:{session_id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": data}


@router.delete("/messages/{session_id}/{message_id}")
def security_delete_message(
    session_id: str,
    message_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    deleted = delete_security_session_message(
        db,
        security_user_id=user.id,
        session_id=session_id,
        message_id=message_id,
    )
    if not deleted:
        raise AppException("Message not found", status_code=404)
    return {"data": {"id": message_id, "deleted": True}}


@router.post("/requests/{session_id}/action")
async def security_request_action(
    session_id: str,
    payload: SecurityActionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security", "homeowner")),
):
    updated = update_security_session_status(
        db,
        session_id=session_id,
        actor=user,
        action=payload.action,
    )
    if not updated:
        raise AppException("Unable to update visitor request", status_code=400)

    serialized = serialize_security_session(db, updated)
    event_name = {
        "forward": "visitor_forwarded",
        "approve": "gate_action_completed",
        "approve_repeat_visitor": "gate_action_completed",
        "reject": "gate_action_completed",
        "delivery_drop_off": "gate_action_completed",
        "confirm_entry": "gate_action_completed",
        "deny_gate": "gate_action_completed",
    }.get((payload.action or "").strip().lower(), "security_request_updated")

    await sio.emit(
        event_name,
        {"data": serialized, "action": payload.action, "actorRole": user.role.value},
        namespace=settings.DASHBOARD_NAMESPACE,
    )
    await sio.emit(
        "session.status",
        {
            "sessionId": updated.id,
            "status": updated.status,
            "gateStatus": updated.gate_status,
            "communicationStatus": updated.communication_status,
            "homeownerId": updated.homeowner_id,
        },
        room=f"session:{updated.id}",
        namespace=settings.SIGNALING_NAMESPACE,
    )
    return {"data": serialized}


@router.post("/access-passes/validate")
def security_validate_access_pass(
    payload: AccessPassValidationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    data = validate_access_pass(
        db,
        security_user_id=user.id,
        estate_id=user.estate_id,
        gate_id=user.gate_id,
        code_value=payload.codeValue,
    )
    return {"data": data}
