from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_roles
from app.core.exceptions import AppException
from app.db.models import ThreatAlertLog, User, UserRole
from app.db.session import get_db
from app.services.advanced_service import (
    build_receipt_pdf_bytes,
    contribute_split_bill,
    create_community_post,
    create_digital_receipt,
    create_snapshot_audit,
    create_split_bill,
    create_threat_alert,
    generate_weekly_summary,
    get_digital_receipt,
    get_split_bill,
    list_community_posts,
    list_digital_receipts,
    list_live_queue,
    load_snapshot_bytes,
    mark_community_post_read,
    notify_multi_channel,
    register_or_recognize_visitor,
    trigger_emergency_signal,
)
from app.socket.server import sio

router = APIRouter()


class RecognitionPayload(BaseModel):
    residentId: str
    displayName: str
    identifier: str = Field(min_length=6)
    encryptedTemplate: Optional[str] = None


class SplitParticipant(BaseModel):
    userId: str
    pledgedAmountKobo: int = 0


class SplitBillCreatePayload(BaseModel):
    title: str
    description: str = ""
    totalAmountKobo: int
    currency: str = "NGN"
    dueAt: Optional[str] = None
    participants: list[SplitParticipant] = []


class SplitContributionPayload(BaseModel):
    amountKobo: int


class ReceiptCreatePayload(BaseModel):
    reference: str
    amountKobo: int
    currency: str = "NGN"
    purpose: str = "general"
    payload: dict[str, Any] = {}


class ThreatPayload(BaseModel):
    homeownerId: str
    visitorSessionId: Optional[str] = None
    riskScore: int = 0
    category: str = "unknown_face"
    message: str = "AI detected unusual visitor behavior."
    snapshotAuditId: Optional[str] = None


class GeofenceCheckPayload(BaseModel):
    checkLat: float
    checkLng: float
    centerLat: float
    centerLng: float
    radiusMeters: int = 50
    homeownerId: Optional[str] = None


class EmergencyPayload(BaseModel):
    scope: str = "estate"
    message: str = "Emergency triggered"
    notifySms: bool = False


class CommunityPostPayload(BaseModel):
    audienceScope: str = "estate"
    title: str
    body: str = ""
    tag: str = "notice"
    pinned: bool = False


@router.get("/visitor/queue")
def advanced_live_queue(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "estate", "admin")),
):
    if user.role == UserRole.homeowner:
        data = list_live_queue(db, homeowner_id=user.id, limit=limit)
    else:
        # For estate/admin sample view, return recent global queue.
        data = []
    return {"data": data}


@router.post("/visitor/snapshots")
async def advanced_upload_snapshot(
    homeownerId: str,
    mediaType: str = "photo",
    visitorSessionId: Optional[str] = None,
    appointmentId: Optional[str] = None,
    source: str = "visitor_device",
    media: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in {UserRole.homeowner, UserRole.estate, UserRole.admin}:
        raise AppException("Insufficient permissions.", status_code=403)
    if user.role == UserRole.homeowner and homeownerId != user.id:
        raise AppException("Not authorized to upload snapshot for another user.", status_code=403)
    if user.role == UserRole.estate:
        # Estate users should not be able to upload into arbitrary homeowner storage.
        raise AppException("Insufficient permissions.", status_code=403)
    media_bytes = await media.read()
    if not media_bytes:
        raise AppException("Empty media upload.", status_code=400)
    data = create_snapshot_audit(
        db,
        homeowner_id=homeownerId,
        media_bytes=media_bytes,
        filename_hint=media.filename or "capture.jpg",
        media_type=mediaType,
        visitor_session_id=visitorSessionId,
        appointment_id=appointmentId,
        source=source,
    )
    notify_multi_channel(
        db,
        user_id=homeownerId,
        kind="visitor.snapshot",
        message="New visitor snapshot received.",
        payload={"snapshotId": data["id"], "mediaType": data["mediaType"]},
    )
    await sio.emit(
        "visitor.snapshot",
        {"data": data},
        room=f"user:{homeownerId}",
    )
    return {"data": data}


@router.get("/visitor/snapshots/{snapshot_id}")
def advanced_download_snapshot(
    snapshot_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    blob, media_type, _content_type = load_snapshot_bytes(
        db,
        snapshot_id=snapshot_id,
        requester_user_id=user.id,
        is_admin=user.role == UserRole.admin,
    )
    return {
        "data": {
            "snapshotId": snapshot_id,
            "mediaType": media_type,
            "bytesBase64": blob.hex(),
            "encoding": "hex",
        }
    }


@router.get("/visitor/snapshots/{snapshot_id}/file")
def advanced_download_snapshot_file(
    snapshot_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    blob, logical_type, content_type = load_snapshot_bytes(
        db,
        snapshot_id=snapshot_id,
        requester_user_id=user.id,
        is_admin=user.role == UserRole.admin,
    )
    return Response(content=blob, media_type=content_type, headers={"Cache-Control": "no-store"})


@router.post("/visitor/recognition")
def advanced_visitor_recognition(
    payload: RecognitionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != UserRole.admin and payload.homeownerId != user.id:
        raise AppException("Not authorized.", status_code=403)
    data = register_or_recognize_visitor(
        db,
        homeowner_id=payload.homeownerId,
        display_name=payload.displayName,
        identifier=payload.identifier,
        encrypted_template=payload.encryptedTemplate,
    )
    return {"data": data}


@router.post("/split-bills")
async def advanced_create_split_bill(
    payload: SplitBillCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "estate")),
):
    due_at = datetime.fromisoformat(payload.dueAt) if payload.dueAt else None
    data = create_split_bill(
        db,
        owner_user_id=user.id,
        title=payload.title,
        description=payload.description,
        total_amount_kobo=payload.totalAmountKobo,
        due_at=due_at,
        participants=[row.model_dump() for row in payload.participants],
        currency=payload.currency,
    )
    await sio.emit("payments.split.updated", {"data": data}, room=f"user:{user.id}")
    return {"data": data}


@router.get("/split-bills/{bill_id}")
def advanced_get_split_bill(
    bill_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": get_split_bill(db, bill_id, requester_user_id=user.id, is_admin=user.role == UserRole.admin)}


@router.post("/split-bills/{bill_id}/contribute")
async def advanced_contribute_split_bill(
    bill_id: str,
    payload: SplitContributionPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = contribute_split_bill(
        db,
        bill_id=bill_id,
        user_id=user.id,
        amount_kobo=payload.amountKobo,
    )
    await sio.emit("payments.split.updated", {"data": data}, room=f"user:{user.id}")
    return {"data": data}


@router.post("/receipts")
def advanced_create_receipt(
    payload: ReceiptCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = create_digital_receipt(
        db,
        owner_user_id=user.id,
        reference=payload.reference,
        amount_kobo=payload.amountKobo,
        currency=payload.currency,
        purpose=payload.purpose,
        payload=payload.payload,
    )
    return {"data": data}


@router.get("/receipts")
def advanced_list_receipts(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": list_digital_receipts(db, owner_user_id=user.id, limit=limit)}


@router.get("/receipts/{receipt_id}/pdf")
def advanced_receipt_pdf(
    receipt_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    receipt = get_digital_receipt(
        db,
        owner_user_id=user.id,
        receipt_id=receipt_id,
        is_admin=user.role == UserRole.admin,
    )
    pdf_bytes = build_receipt_pdf_bytes(
        receipt,
        owner_name=user.full_name,
        owner_email=user.email,
    )
    filename = f"qring-receipt-{receipt.get('reference') or receipt_id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.post("/security/threat-alert")
async def advanced_threat_alert(
    payload: ThreatPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "estate", "admin")),
):
    if user.role == UserRole.estate:
        target_id = str(payload.homeownerId or "").strip()
        if not target_id:
            raise AppException("homeownerId is required.", status_code=400)
        target = db.query(User).filter(User.id == target_id, User.role == UserRole.homeowner).first()
        if not target or not target.estate_id:
            raise AppException("Not authorized.", status_code=403)
        from app.db.models import Estate

        estate = db.query(Estate).filter(Estate.id == target.estate_id, Estate.owner_id == user.id).first()
        if not estate:
            raise AppException("Not authorized.", status_code=403)
    data = create_threat_alert(
        db,
        homeowner_id=payload.homeownerId if user.role != UserRole.homeowner else user.id,
        visitor_session_id=payload.visitorSessionId,
        risk_score=payload.riskScore,
        category=payload.category,
        message=payload.message,
        snapshot_audit_id=payload.snapshotAuditId,
    )
    await sio.emit("security.threat_alert", {"data": data}, room=f"user:{data['homeownerId']}")
    return {"data": data}


@router.get("/security/threat-alerts")
def advanced_list_threat_alerts(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "estate", "admin")),
):
    q = db.query(ThreatAlertLog).order_by(ThreatAlertLog.created_at.desc()).limit(max(1, min(limit, 300)))
    if user.role == UserRole.homeowner:
        q = q.filter(ThreatAlertLog.homeowner_id == user.id)
    rows = q.all()
    return {
        "data": [
            {
                "id": row.id,
                "homeownerId": row.homeowner_id,
                "visitorSessionId": row.visitor_session_id,
                "riskScore": row.risk_score,
                "category": row.category,
                "message": row.message,
                "snapshotAuditId": row.snapshot_audit_id,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.post("/security/geofence-check")
def advanced_geofence_check(
    payload: GeofenceCheckPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.appointment_service import _distance_meters

    radius = max(30, min(int(payload.radiusMeters or 50), 2000))
    distance = _distance_meters(payload.checkLat, payload.checkLng, payload.centerLat, payload.centerLng)
    inside = distance <= radius
    if not inside and payload.homeownerId:
        notify_multi_channel(
            db,
            user_id=payload.homeownerId,
            kind="security.geofence_violation",
            message=f"Check-in attempt outside geofence ({int(distance)}m).",
            payload={"distanceMeters": int(distance), "radiusMeters": radius},
        )
    return {"data": {"inside": inside, "distanceMeters": int(distance), "radiusMeters": radius}}


@router.post("/security/emergency")
async def advanced_emergency(
    payload: EmergencyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = trigger_emergency_signal(
        db,
        requester_user_id=user.id,
        scope=payload.scope,
        message=payload.message,
        notify_sms=payload.notifySms,
    )
    await sio.emit("security.emergency", {"data": data}, room=f"user:{user.id}")
    return {"data": data}


@router.post("/community/posts")
async def advanced_create_community_post(
    payload: CommunityPostPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner", "estate", "admin")),
):
    data = create_community_post(
        db,
        author_user_id=user.id,
        audience_scope=payload.audienceScope,
        title=payload.title,
        body=payload.body,
        tag=payload.tag,
        pinned=payload.pinned,
    )
    await sio.emit("community.post.created", {"data": data})
    return {"data": data}


@router.get("/community/posts")
def advanced_list_community_posts(
    scope: str = "estate",
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": list_community_posts(db, reader_user_id=user.id, audience_scope=scope, limit=limit)}


@router.post("/community/posts/{post_id}/read")
def advanced_mark_community_post_read(
    post_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": mark_community_post_read(db, post_id=post_id, reader_user_id=user.id)}


@router.get("/summaries/weekly")
def advanced_weekly_summary(
    weekStartIso: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = generate_weekly_summary(db, user_id=user.id, week_start_iso=weekStartIso)
    notify_multi_channel(
        db,
        user_id=user.id,
        kind="summary.weekly",
        message="Your weekly Qring summary is ready.",
        payload=data,
    )
    return {"data": data}
