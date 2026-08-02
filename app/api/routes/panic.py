from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.core.exceptions import AppException
from app.db.models import User
from app.db.session import get_db
from app.services.safety_service import (
    acknowledge_panic_event,
    create_panic_audio_segment,
    list_active_panic_events,
    list_panic_audio_segments,
    load_panic_audio_segment_bytes,
    report_false_panic_event,
    resolve_panic_event,
    respond_to_panic_event,
    trigger_panic_event,
    update_panic_event_notes,
)

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


class PanicActionPayload(BaseModel):
    panicId: str


class PanicNotesPayload(BaseModel):
    panicId: str
    notes: str = ""


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


@router.post("/respond")
def panic_respond(
    payload: PanicActionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": respond_to_panic_event(db, panic_id=payload.panicId, actor=user)}


@router.post("/ignore")
def panic_ignore(
    payload: PanicActionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": ignore_panic_event(db, panic_id=payload.panicId, actor=user)}


@router.post("/report-false")
def panic_report_false(
    payload: PanicActionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": report_false_panic_event(db, panic_id=payload.panicId, actor=user)}


@router.post("/audio/segment")
async def panic_upload_audio_segment(
    panicId: str,
    segmentIndex: int = 0,
    filenameHint: Optional[str] = None,
    media: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "admin")),
):
    media_bytes = await media.read()
    if not media_bytes:
        raise AppException("Empty audio upload.", status_code=400)
    data = create_panic_audio_segment(
        db,
        actor=user,
        panic_id=panicId,
        segment_index=segmentIndex,
        media_bytes=media_bytes,
        filename_hint=filenameHint or media.filename or "segment.webm",
        media_type=media.content_type or "audio/webm",
    )
    return {"data": data}


@router.get("/{panic_id}/audio/segments")
def panic_list_audio_segments(
    panic_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": list_panic_audio_segments(db, panic_id=panic_id, actor=user)}


@router.get("/audio/segment/{segment_id}/file")
def panic_download_audio_segment(
    segment_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    data, content_type = load_panic_audio_segment_bytes(db, segment_id=segment_id, actor=user)
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "no-store"})


@router.post("/notes")
def panic_notes(
    payload: PanicNotesPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security", "estate", "admin")),
):
    return {"data": update_panic_event_notes(db, panic_id=payload.panicId, actor=user, notes=payload.notes)}


@router.get("/active")
def panic_active(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "security", "estate", "admin")),
):
    return {"data": list_active_panic_events(db, actor=user)}
