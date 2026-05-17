from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.db.models import CallSession

logger = logging.getLogger(__name__)


def _participant_count(payload: dict[str, Any], room: dict[str, Any]) -> int | None:
    for source in (payload, room):
        if not isinstance(source, dict):
            continue
        raw = source.get("numParticipants", source.get("num_participants"))
        if raw in (None, ""):
            continue
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            return count
    return None


def handle_livekit_webhook_event(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("event") or "").strip().lower()
    room = payload.get("room") if isinstance(payload.get("room"), dict) else {}
    room_name = str(room.get("name") or payload.get("roomName") or "").strip()
    participant = payload.get("participant") if isinstance(payload.get("participant"), dict) else {}
    participant_identity = str(participant.get("identity") or "").strip()
    participant_count = _participant_count(payload, room)
    track = payload.get("track") if isinstance(payload.get("track"), dict) else {}
    track_sid = str(track.get("sid") or "").strip()
    track_type = str(track.get("type") or track.get("source") or "").strip()

    if event in {"participant_joined", "participant_left", "room_finished", "room_started", "track_published", "track_unpublished"}:
        logger.info(
            "livekit.webhook event=%s room_name=%s participant_identity=%s participant_count=%s track_sid=%s track_type=%s",
            event,
            room_name,
            participant_identity,
            participant_count,
            track_sid,
            track_type,
        )
    else:
        logger.info("livekit.webhook event=%s room_name=%s", event, room_name)

    if not room_name:
        return {"event": event, "roomName": "", "handled": False}

    call = db.query(CallSession).filter(CallSession.room_name == room_name).first()
    if not call:
        return {"event": event, "roomName": room_name, "handled": False}

    if (
        event == "participant_joined"
        and call.status in {"pending", "ringing"}
        and participant_count is not None
        and participant_count >= 2
    ):
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
        "participantCount": participant_count,
        "trackSid": track_sid,
        "trackType": track_type,
    }
