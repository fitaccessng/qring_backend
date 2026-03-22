from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.db.models import CallSession

logger = logging.getLogger(__name__)


def handle_livekit_webhook_event(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("event") or "").strip().lower()
    room = payload.get("room") if isinstance(payload.get("room"), dict) else {}
    room_name = str(room.get("name") or payload.get("roomName") or "").strip()
    participant = payload.get("participant") if isinstance(payload.get("participant"), dict) else {}
    participant_identity = str(participant.get("identity") or "").strip()

    if event in {"participant_joined", "participant_left", "room_finished"}:
        logger.info(
            "livekit.webhook event=%s room_name=%s participant_identity=%s",
            event,
            room_name,
            participant_identity,
        )
    else:
        logger.info("livekit.webhook event=%s room_name=%s", event, room_name)

    if not room_name:
        return {"event": event, "roomName": "", "handled": False}

    call = db.query(CallSession).filter(CallSession.room_name == room_name).first()
    if not call:
        return {"event": event, "roomName": room_name, "handled": False}

    if event == "participant_joined" and call.status in {"pending", "ringing"}:
        call.status = "ongoing"
        call.answered_at = call.answered_at or utc_now()
        db.commit()
        db.refresh(call)
    elif event == "room_finished":
        call.status = "missed" if call.status in {"pending", "ringing"} else "ended"
        call.ended_at = call.ended_at or utc_now()
        call.ended_reason = call.ended_reason or ("room_finished_before_answer" if call.status == "missed" else "room_finished")
        db.commit()
        db.refresh(call)

    return {
        "event": event,
        "roomName": room_name,
        "handled": True,
        "callSessionId": call.id,
        "status": call.status,
    }
