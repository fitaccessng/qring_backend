from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, Request
from typing import Optional
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_optional_current_user
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import CallSession, User
from app.db.session import get_db
from app.services.livekit_service import (
    build_livekit_identity,
    build_request_room_name,
    issue_livekit_token_for_room,
)
from app.services.livekit_monitor_service import handle_livekit_webhook_event

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


class LivekitTokenPayload(BaseModel):
    user_id: str
    role: str
    visitor_request_id: str

    @field_validator("user_id", "role", "visitor_request_id", mode="before")
    @classmethod
    def validate_text_fields(cls, value):
        text = str(value or "").strip()
        if not text:
            raise ValueError("field is required")
        return text


def _authorize_livekit_token_request(
    db: Session,
    *,
    payload: LivekitTokenPayload,
    user: Optional[User],
) -> Optional[CallSession]:
    normalized_role = payload.role.lower()
    if normalized_role not in {"homeowner", "security", "visitor"}:
        raise AppException("role must be homeowner, security or visitor.", status_code=400)

    call = (
        db.query(CallSession)
        .filter(CallSession.visitor_request_id == payload.visitor_request_id)
        .order_by(CallSession.created_at.desc())
        .first()
    )

    if normalized_role in {"homeowner", "security"}:
        if not user:
            raise AppException("Authentication is required.", status_code=401)
        if user.role.value != normalized_role or user.id != payload.user_id:
            raise AppException("You are not allowed to request this token.", status_code=403)
        if call:
            expected_user_id = call.homeowner_id if normalized_role == "homeowner" else call.security_user_id
            if expected_user_id and expected_user_id != user.id:
                raise AppException("You are not allowed to join this visitor request.", status_code=403)
        return call

    if call and call.visitor_id != payload.user_id:
        raise AppException("You are not allowed to join this visitor request.", status_code=403)
    return call


def _is_valid_signature(raw_body: bytes, signature: Optional[str]) -> bool:
    secret = (settings.LIVEKIT_WEBHOOK_SECRET or "").strip()
    if not secret:
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


@router.post("/webhooks/livekit")
async def livekit_webhook(
    request: Request,
    x_livekit_signature: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    raw_body = await request.body()
    if not _is_valid_signature(raw_body, x_livekit_signature):
        logger.warning("livekit.webhook.invalid_signature")
        raise AppException("Invalid LiveKit webhook signature.", status_code=401)

    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception as exc:
        raise AppException("Invalid webhook payload.", status_code=400) from exc

    result = handle_livekit_webhook_event(db, payload if isinstance(payload, dict) else {})
    return {"data": result}


@router.post("/get-livekit-token")
async def get_livekit_token(
    payload: LivekitTokenPayload,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_optional_current_user),
):
    call = _authorize_livekit_token_request(db, payload=payload, user=user)
    room_name = build_request_room_name(payload.visitor_request_id)
    issued = issue_livekit_token_for_room(
        room_name=room_name,
        identity=build_livekit_identity(payload.role, payload.user_id),
        display_name=(user.full_name if user else payload.role.title()) if payload.role != "visitor" else "Visitor",
        can_publish=True,
        can_subscribe=True,
    )
    return {
        "data": {
            "token": issued["token"],
            "roomName": issued["roomName"],
            "url": issued["url"],
            "expiresIn": issued["expiresIn"],
            "status": call.status if call else "ringing",
        }
    }
