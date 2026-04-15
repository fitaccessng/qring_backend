from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.time import utc_now
from app.db.models import (
    Appointment,
    CommunityPost,
    CommunityPostRead,
    DigitalReceipt,
    EmergencySignal,
    Notification,
    SplitBill,
    SplitContribution,
    ThreatAlertLog,
    User,
    UserRole,
    VisitorRecognitionProfile,
    VisitorSession,
    VisitorSnapshotAudit,
    WeeklySummaryLog,
)
from app.services.notification_service import create_notification
from app.services.provider_integrations import (
    get_user_contact,
    recognize_face_provider,
    send_email_smtp,
    send_push_fcm,
    send_sms_provider,
)

settings = get_settings()

_firebase_lock = Lock()
_firebase_app = None

try:
    import firebase_admin
    from firebase_admin import credentials as firebase_credentials
    from firebase_admin import storage as firebase_storage
except ImportError:
    firebase_admin = None
    firebase_credentials = None
    firebase_storage = None


def _load_firebase_credentials() -> dict | None:
    raw_json = (settings.FIREBASE_SERVICE_ACCOUNT_JSON or "").strip()
    if raw_json:
        return json.loads(raw_json)
    raw_base64 = (settings.FIREBASE_SERVICE_ACCOUNT_BASE64 or "").strip()
    if raw_base64:
        import base64

        decoded = base64.b64decode(raw_base64).decode("utf-8")
        return json.loads(decoded)
    return None


def _resolve_storage_bucket() -> str | None:
    bucket = (settings.FIREBASE_STORAGE_BUCKET or "").strip()
    if bucket:
        return bucket
    project_id = (settings.FIREBASE_PROJECT_ID or "").strip()
    if project_id:
        return f"{project_id}.appspot.com"
    return None


def _get_firebase_app():
    global _firebase_app
    if firebase_admin is None or firebase_credentials is None or firebase_storage is None:
        return None
    if _firebase_app is not None:
        return _firebase_app
    with _firebase_lock:
        if _firebase_app is not None:
            return _firebase_app
        bucket_name = _resolve_storage_bucket()
        if not bucket_name:
            return None
        if firebase_admin._apps:
            _firebase_app = firebase_admin.get_app()
            return _firebase_app
        creds = _load_firebase_credentials()
        if creds:
            _firebase_app = firebase_admin.initialize_app(
                credential=firebase_credentials.Certificate(creds),
                options={
                    "projectId": settings.FIREBASE_PROJECT_ID,
                    "storageBucket": bucket_name,
                },
            )
            return _firebase_app
        _firebase_app = firebase_admin.initialize_app(
            options={
                "projectId": settings.FIREBASE_PROJECT_ID,
                "storageBucket": bucket_name,
            }
        )
        return _firebase_app


def _get_storage_bucket():
    if firebase_storage is None:
        return None
    bucket_name = _resolve_storage_bucket()
    if not bucket_name:
        return None
    app = _get_firebase_app()
    if app is None:
        return None
    return firebase_storage.bucket(bucket_name, app=app)


def _to_kobo(amount_naira: float | int) -> int:
    return int(round(float(amount_naira) * 100))


def _media_base_dir() -> Path:
    raw = (settings.MEDIA_STORAGE_PATH or "").strip()
    if raw:
        base = Path(raw)
    else:
        base = Path(__file__).resolve().parents[2] / "uploads" / "visitor-media"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _summary_window(week_start_iso: str | None = None) -> tuple[datetime, datetime]:
    if week_start_iso:
        start = datetime.fromisoformat(week_start_iso.replace("Z", "+00:00")).replace(tzinfo=None)
        end = start + timedelta(days=7)
    else:
        now = utc_now()
        start = now - timedelta(days=7)
        end = now
    return start, end


def notify_multi_channel(
    db: Session,
    *,
    user_id: str,
    kind: str,
    message: str,
    payload: dict[str, Any] | None = None,
    use_sms: bool = False,
) -> dict[str, Any]:
    combined = dict(payload or {})
    combined["message"] = message
    create_notification(db=db, user_id=user_id, kind=kind, payload=combined)
    user = get_user_contact(db, user_id=user_id)
    push_result = send_push_fcm(
        db,
        user_id=user_id,
        title="Qring Alert",
        body=message,
        data={"kind": kind, **{k: str(v) for k, v in (payload or {}).items()}},
    )
    email_result = (
        send_email_smtp(to_email=user.email, subject="Qring Alert", body=message)
        if user and user.email
        else {"status": "skipped", "reason": "missing_email"}
    )
    sms_result = {"status": "skipped"}
    if use_sms:
        phone = str((payload or {}).get("phoneNumber") or (payload or {}).get("phone") or "").strip()
        sms_result = send_sms_provider(phone_number=phone, message=message)
    return {"push": push_result, "email": email_result, "sms": sms_result}


def create_snapshot_audit(
    db: Session,
    *,
    resident_id: str,
    media_bytes: bytes,
    filename_hint: str,
    media_type: str,
    visitor_session_id: str | None = None,
    appointment_id: str | None = None,
    source: str = "visitor_device",
) -> dict[str, Any]:
    ext = Path(filename_hint or "capture.jpg").suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm"}:
        ext = ".bin"
    media_id = str(uuid.uuid4())
    storage_bucket = _get_storage_bucket()
    media_path = ""
    if storage_bucket is not None:
        storage_path = f"visitor-media/{resident_id}/{media_id}{ext}"
        try:
            blob = storage_bucket.blob(storage_path)
            blob.cache_control = "private, max-age=0, no-transform"
            blob.metadata = {
                "residentId": resident_id,
                "mediaId": media_id,
                "visitorSessionId": visitor_session_id or "",
                "appointmentId": appointment_id or "",
                "mediaType": (media_type or "photo").strip().lower(),
            }
            blob.upload_from_string(media_bytes, content_type="application/octet-stream")
            media_path = f"firebase:{storage_path}"
        except Exception:
            # Fall back to local storage if cloud upload fails.
            media_path = ""

    if not media_path:
        relative_path = Path(resident_id) / f"{media_id}{ext}"
        absolute_path = _media_base_dir() / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(media_bytes)
        media_path = str(relative_path).replace("\\", "/")

    digest = hashlib.sha256(media_bytes).hexdigest()
    row = VisitorSnapshotAudit(
        resident_id=resident_id,
        visitor_session_id=visitor_session_id,
        appointment_id=appointment_id,
        media_type=(media_type or "photo").strip().lower(),
        media_path=media_path,
        media_sha256=digest,
        source=source,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "residentId": row.resident_id,
        "visitorSessionId": row.visitor_session_id,
        "appointmentId": row.appointment_id,
        "mediaType": row.media_type,
        "mediaPath": row.media_path,
        "mediaSha256": row.media_sha256,
        "source": row.source,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def _snapshot_content_type_from_path(path: str, logical_type: str) -> str:
    ext = Path(path or "").suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    if ext in {".mp4"}:
        return "video/mp4"
    if ext in {".webm"}:
        return "video/webm"
    if ext in {".mov"}:
        return "video/quicktime"
    if logical_type == "photo":
        return "image/jpeg"
    if logical_type == "video":
        return "video/mp4"
    return "application/octet-stream"


def load_snapshot_bytes(
    db: Session, *, snapshot_id: str, requester_user_id: str, is_admin: bool
) -> tuple[bytes, str, str]:
    row = db.query(VisitorSnapshotAudit).filter(VisitorSnapshotAudit.id == snapshot_id).first()
    if not row:
        raise AppException("Snapshot not found.", status_code=404)
    if not is_admin and row.resident_id != requester_user_id:
        raise AppException("Not authorized to access this snapshot.", status_code=403)

    media_path = str(row.media_path or "")
    logical_type = row.media_type if row.media_type in {"photo", "video"} else "binary"
    content_type = _snapshot_content_type_from_path(media_path, logical_type)
    if media_path.startswith("firebase:"):
        storage_path = media_path.split("firebase:", 1)[1]
        bucket = _get_storage_bucket()
        if bucket is None:
            raise AppException("Snapshot storage is unavailable.", status_code=404)
        blob = bucket.blob(storage_path)
        try:
            blob.reload()
        except Exception as exc:
            raise AppException("Snapshot file is unavailable.", status_code=404) from exc
        try:
            data = blob.download_as_bytes()
        except Exception as exc:
            raise AppException("Snapshot file is unavailable.", status_code=404) from exc
        content_type = _snapshot_content_type_from_path(storage_path, logical_type)
        return data, logical_type, content_type

    path = _media_base_dir() / media_path
    if not path.exists():
        raise AppException("Snapshot file is unavailable.", status_code=404)
    return path.read_bytes(), logical_type, content_type


def list_live_queue(db: Session, *, resident_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = (
        db.query(VisitorSession)
        .filter(VisitorSession.homeowner_id == homeowner_id)
        .order_by(VisitorSession.started_at.desc())
        .limit(max(1, min(int(limit), 300)))
        .all()
    )
    appointment_ids = [row.appointment_id for row in rows if row.appointment_id]
    appointments: dict[str, Appointment] = {}
    if appointment_ids:
        for appt in db.query(Appointment).filter(Appointment.id.in_(appointment_ids)).all():
            appointments[appt.id] = appt
    return [
        {
            "sessionId": row.id,
            "visitorName": row.visitor_label or (appointments.get(row.appointment_id).visitor_name if row.appointment_id in appointments else "Visitor"),
            "arrivalTime": row.started_at.isoformat() if row.started_at else None,
            "approvalStatus": row.status,
            "appointmentId": row.appointment_id,
        }
        for row in rows
    ]


def register_or_recognize_visitor(
    db: Session,
    *,
    homeowner_id: str,
    display_name: str,
    identifier: str,
    encrypted_template: str | None = None,
) -> dict[str, Any]:
    provider_response = recognize_face_provider(
        homeowner_id=homeowner_id,
        display_name=display_name,
        identifier=identifier,
        encrypted_template=encrypted_template,
    )
    effective_identifier = identifier
    if isinstance(provider_response, dict):
        provider_id = str(provider_response.get("visitorId") or "").strip()
        if provider_id:
            effective_identifier = provider_id

    key_hash = hashlib.sha256(effective_identifier.strip().encode("utf-8")).hexdigest()
    row = (
        db.query(VisitorRecognitionProfile)
        .filter(
            VisitorRecognitionProfile.homeowner_id == homeowner_id,
            VisitorRecognitionProfile.visitor_key_hash == key_hash,
        )
        .first()
    )
    returning = bool(row)
    if row:
        row.visits_count = int(row.visits_count or 0) + 1
        row.last_seen_at = utc_now()
        if display_name:
            row.display_name = display_name.strip()
        if encrypted_template:
            row.encrypted_template = encrypted_template
    else:
        row = VisitorRecognitionProfile(
            homeowner_id=homeowner_id,
            display_name=display_name.strip() or "Visitor",
            visitor_key_hash=key_hash,
            encrypted_template=encrypted_template,
            visits_count=1,
            last_seen_at=utc_now(),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "profileId": row.id,
        "returningVisitor": returning,
        "displayName": row.display_name,
        "visitsCount": int(row.visits_count or 0),
        "providerMatched": bool(provider_response and provider_response.get("matched")),
        "providerConfidence": provider_response.get("confidence") if isinstance(provider_response, dict) else None,
        "skipApprovalSuggestion": returning and int(row.visits_count or 0) >= 2,
        "message": f"Welcome back, {row.display_name}! Do you want to skip approval?"
        if returning
        else f"New visitor profile created for {row.display_name}.",
    }


def create_split_bill(
    db: Session,
    *,
    owner_user_id: str,
    title: str,
    description: str,
    total_amount_kobo: int,
    due_at: datetime | None,
    participants: list[dict[str, Any]],
    currency: str = "NGN",
) -> dict[str, Any]:
    if total_amount_kobo <= 0:
        raise AppException("Total amount must be greater than zero.", status_code=400)
    bill = SplitBill(
        owner_user_id=owner_user_id,
        title=title.strip() or "Shared bill",
        description=description.strip(),
        total_amount_kobo=total_amount_kobo,
        currency=(currency or "NGN").upper(),
        due_at=due_at,
    )
    db.add(bill)
    db.flush()
    for entry in participants:
        user_id = str(entry.get("userId") or "").strip()
        if not user_id:
            continue
        pledged = int(entry.get("pledgedAmountKobo") or 0)
        contribution = SplitContribution(
            split_bill_id=bill.id,
            contributor_user_id=user_id,
            pledged_amount_kobo=max(0, pledged),
            paid_amount_kobo=0,
            status="pending",
        )
        db.add(contribution)
        notify_multi_channel(
            db,
            user_id=user_id,
            kind="payment.split_due",
            message=f"New shared bill: {bill.title}",
            payload={"splitBillId": bill.id, "dueAt": bill.due_at.isoformat() if bill.due_at else None},
        )
    db.commit()
    return get_split_bill(db, bill.id, requester_user_id=owner_user_id)


def get_split_bill(
    db: Session,
    bill_id: str,
    *,
    requester_user_id: str | None = None,
    is_admin: bool = False,
) -> dict[str, Any]:
    bill = db.query(SplitBill).filter(SplitBill.id == bill_id).first()
    if not bill:
        raise AppException("Split bill not found.", status_code=404)
    if not is_admin and requester_user_id:
        if bill.owner_user_id != requester_user_id:
            allowed = (
                db.query(SplitContribution.id)
                .filter(
                    SplitContribution.split_bill_id == bill.id,
                    SplitContribution.contributor_user_id == requester_user_id,
                )
                .first()
            )
            if not allowed:
                raise AppException("Not authorized to access this split bill.", status_code=403)
    if not is_admin and not requester_user_id:
        raise AppException("Not authorized to access this split bill.", status_code=403)
    rows = (
        db.query(SplitContribution)
        .filter(SplitContribution.split_bill_id == bill.id)
        .order_by(SplitContribution.created_at.asc())
        .all()
    )
    paid_total = sum(int(row.paid_amount_kobo or 0) for row in rows)
    if paid_total >= int(bill.total_amount_kobo or 0):
        bill.status = "completed"
        db.commit()
    return {
        "id": bill.id,
        "title": bill.title,
        "description": bill.description,
        "totalAmountKobo": int(bill.total_amount_kobo or 0),
        "paidAmountKobo": paid_total,
        "remainingAmountKobo": max(0, int(bill.total_amount_kobo or 0) - paid_total),
        "currency": bill.currency,
        "status": bill.status,
        "dueAt": bill.due_at.isoformat() if bill.due_at else None,
        "contributions": [
            {
                "id": row.id,
                "userId": row.contributor_user_id,
                "pledgedAmountKobo": int(row.pledged_amount_kobo or 0),
                "paidAmountKobo": int(row.paid_amount_kobo or 0),
                "status": row.status,
                "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ],
    }


def contribute_split_bill(db: Session, *, bill_id: str, user_id: str, amount_kobo: int) -> dict[str, Any]:
    if amount_kobo <= 0:
        raise AppException("Contribution amount must be greater than zero.", status_code=400)
    row = (
        db.query(SplitContribution)
        .filter(
            SplitContribution.split_bill_id == bill_id,
            SplitContribution.contributor_user_id == user_id,
        )
        .first()
    )
    if not row:
        raise AppException("Contributor is not registered for this bill.", status_code=404)
    row.paid_amount_kobo = int(row.paid_amount_kobo or 0) + int(amount_kobo)
    row.status = "paid" if row.paid_amount_kobo >= int(row.pledged_amount_kobo or 0) else "partial"
    db.commit()
    return get_split_bill(db, bill_id, requester_user_id=user_id)


def create_digital_receipt(
    db: Session,
    *,
    owner_user_id: str,
    reference: str,
    amount_kobo: int,
    currency: str,
    purpose: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    row = DigitalReceipt(
        owner_user_id=owner_user_id,
        reference=reference.strip() or f"receipt-{uuid.uuid4().hex[:10]}",
        amount_kobo=max(0, int(amount_kobo)),
        currency=(currency or "NGN").upper(),
        purpose=purpose.strip() or "general",
        payload_json=json.dumps(payload or {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "ownerUserId": row.owner_user_id,
        "reference": row.reference,
        "amountKobo": int(row.amount_kobo or 0),
        "currency": row.currency,
        "purpose": row.purpose,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "payload": payload or {},
    }


def list_digital_receipts(db: Session, *, owner_user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = (
        db.query(DigitalReceipt)
        .filter(DigitalReceipt.owner_user_id == owner_user_id)
        .order_by(DigitalReceipt.created_at.desc())
        .limit(max(1, min(int(limit), 300)))
        .all()
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except Exception:
            payload = {}
        items.append(
            {
                "id": row.id,
                "reference": row.reference,
                "amountKobo": int(row.amount_kobo or 0),
                "currency": row.currency,
                "purpose": row.purpose,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
                "payload": payload,
            }
        )
    return items


def get_digital_receipt(db: Session, *, owner_user_id: str, receipt_id: str, is_admin: bool = False) -> dict[str, Any]:
    row = db.query(DigitalReceipt).filter(DigitalReceipt.id == receipt_id).first()
    if not row:
        raise AppException("Receipt not found.", status_code=404)
    if not is_admin and row.owner_user_id != owner_user_id:
        raise AppException("Not authorized to access this receipt.", status_code=403)
    try:
        payload = json.loads(row.payload_json or "{}")
    except Exception:
        payload = {}
    return {
        "id": row.id,
        "ownerUserId": row.owner_user_id,
        "reference": row.reference,
        "amountKobo": int(row.amount_kobo or 0),
        "currency": row.currency,
        "purpose": row.purpose,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "payload": payload,
    }


def _pdf_escape(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_receipt_pdf_bytes(receipt: dict[str, Any], owner_name: str, owner_email: str) -> bytes:
    amount = f"{(receipt.get('currency') or 'NGN').upper()} {(int(receipt.get('amountKobo') or 0) / 100):,.2f}"
    lines = [
        "Qring Digital Receipt",
        "",
        f"Reference: {receipt.get('reference') or '-'}",
        f"Date: {receipt.get('createdAt') or '-'}",
        f"Purpose: {receipt.get('purpose') or '-'}",
        f"Amount: {amount}",
        "",
        f"Homeowner: {owner_name or '-'}",
        f"Email: {owner_email or '-'}",
    ]
    text_stream = "BT /F1 12 Tf 50 780 Td 14 TL " + " ".join(f"({_pdf_escape(line)}) Tj T*" for line in lines) + " ET"
    content = text_stream.encode("latin-1", errors="replace")

    objects: list[bytes] = []
    objects.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    objects.append(b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    objects.append(
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
    )
    objects.append(b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    objects.append(f"5 0 obj << /Length {len(content)} >> stream\n".encode("latin-1") + content + b"\nendstream endobj\n")

    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(result))
        result.extend(obj)
    xref_pos = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    result.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        result.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    result.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
    )
    return bytes(result)


def create_threat_alert(
    db: Session,
    *,
    homeowner_id: str,
    visitor_session_id: str | None,
    risk_score: int,
    category: str,
    message: str,
    snapshot_audit_id: str | None = None,
) -> dict[str, Any]:
    if visitor_session_id:
        session = db.query(VisitorSession).filter(VisitorSession.id == visitor_session_id).first()
        if not session or session.homeowner_id != homeowner_id:
            raise AppException("Not authorized to attach this visitor session.", status_code=403)
    row = ThreatAlertLog(
        homeowner_id=homeowner_id,
        visitor_session_id=visitor_session_id,
        risk_score=max(0, min(int(risk_score), 100)),
        category=category.strip() or "unknown_face",
        message=message.strip() or "AI detected unusual visitor behavior.",
        snapshot_audit_id=snapshot_audit_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    notify_multi_channel(
        db,
        user_id=homeowner_id,
        kind="security.threat_alert",
        message=row.message,
        payload={
            "threatAlertId": row.id,
            "riskScore": row.risk_score,
            "category": row.category,
            "snapshotAuditId": snapshot_audit_id,
        },
    )
    return {
        "id": row.id,
        "homeownerId": row.homeowner_id,
        "visitorSessionId": row.visitor_session_id,
        "riskScore": row.risk_score,
        "category": row.category,
        "message": row.message,
        "snapshotAuditId": row.snapshot_audit_id,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def trigger_emergency_signal(
    db: Session,
    *,
    requester_user_id: str,
    scope: str,
    message: str,
    notify_sms: bool,
) -> dict[str, Any]:
    row = EmergencySignal(
        requester_user_id=requester_user_id,
        scope=scope.strip() or "estate",
        message=message.strip() or "Emergency triggered",
        notify_sms=bool(notify_sms),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    requester = db.query(User).filter(User.id == requester_user_id).first()
    if requester:
        # Scope notifications to the requester's tenant context to avoid cross-estate leakage.
        recipient_ids: set[str] = {requester.id}
        effective_scope = (scope or "estate").strip().lower()

        if effective_scope == "global" and requester.role == UserRole.admin:
            for user in db.query(User).filter(User.role.in_([UserRole.homeowner, UserRole.security, UserRole.estate])).all():
                recipient_ids.add(user.id)
        else:
            estate_ids: list[str] = []
            if requester.role in {UserRole.homeowner, UserRole.security} and requester.estate_id:
                estate_ids = [requester.estate_id]
            elif requester.role == UserRole.estate:
                from app.db.models import Estate

                estate_ids = [e.id for e in db.query(Estate).filter(Estate.owner_id == requester.id).all()]

            if estate_ids:
                for user_id in (
                    db.query(User.id)
                    .filter(User.estate_id.in_(estate_ids), User.role.in_([UserRole.homeowner, UserRole.security]))
                    .all()
                ):
                    if user_id and user_id[0]:
                        recipient_ids.add(user_id[0])
                # Also notify estate owners of the involved estates.
                from app.db.models import Estate

                owner_rows = db.query(Estate.owner_id).filter(Estate.id.in_(estate_ids)).all()
                for owner_id in [r[0] for r in owner_rows if r and r[0]]:
                    recipient_ids.add(owner_id)

        for recipient_id in recipient_ids:
            notify_multi_channel(
                db,
                user_id=recipient_id,
                kind="security.emergency",
                message=f"Emergency alert from {requester.full_name or 'Resident'}: {row.message}",
                payload={"emergencySignalId": row.id, "scope": row.scope},
                use_sms=bool(notify_sms),
            )
    return {
        "id": row.id,
        "requesterUserId": row.requester_user_id,
        "scope": row.scope,
        "message": row.message,
        "notifySms": row.notify_sms,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def create_community_post(
    db: Session,
    *,
    author_user_id: str,
    audience_scope: str,
    title: str,
    body: str,
    tag: str,
    pinned: bool = False,
) -> dict[str, Any]:
    row = CommunityPost(
        author_user_id=author_user_id,
        audience_scope=audience_scope.strip() or "estate",
        title=title.strip() or "Community Notice",
        body=body.strip(),
        tag=tag.strip() or "notice",
        pinned=bool(pinned),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "authorUserId": row.author_user_id,
        "audienceScope": row.audience_scope,
        "title": row.title,
        "body": row.body,
        "tag": row.tag,
        "pinned": row.pinned,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def list_community_posts(
    db: Session,
    *,
    reader_user_id: str,
    audience_scope: str = "estate",
    limit: int = 100,
) -> list[dict[str, Any]]:
    posts = (
        db.query(CommunityPost)
        .filter(CommunityPost.audience_scope == audience_scope)
        .order_by(CommunityPost.pinned.desc(), CommunityPost.created_at.desc())
        .limit(max(1, min(int(limit), 300)))
        .all()
    )
    post_ids = [p.id for p in posts]
    read_ids: set[str] = set()
    if post_ids:
        rows = (
            db.query(CommunityPostRead)
            .filter(
                CommunityPostRead.reader_user_id == reader_user_id,
                CommunityPostRead.post_id.in_(post_ids),
            )
            .all()
        )
        read_ids = {r.post_id for r in rows}
    return [
        {
            "id": post.id,
            "title": post.title,
            "body": post.body,
            "tag": post.tag,
            "pinned": bool(post.pinned),
            "audienceScope": post.audience_scope,
            "createdAt": post.created_at.isoformat() if post.created_at else None,
            "read": post.id in read_ids,
        }
        for post in posts
    ]


def mark_community_post_read(db: Session, *, post_id: str, reader_user_id: str) -> dict[str, Any]:
    row = (
        db.query(CommunityPostRead)
        .filter(
            CommunityPostRead.post_id == post_id,
            CommunityPostRead.reader_user_id == reader_user_id,
        )
        .first()
    )
    if not row:
        row = CommunityPostRead(post_id=post_id, reader_user_id=reader_user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return {
        "postId": row.post_id,
        "readerUserId": row.reader_user_id,
        "readAt": row.read_at.isoformat() if row.read_at else None,
    }


def generate_weekly_summary(db: Session, *, user_id: str, week_start_iso: str | None = None) -> dict[str, Any]:
    start, end = _summary_window(week_start_iso)
    visitors = (
        db.query(func.count(VisitorSession.id))
        .filter(
            VisitorSession.homeowner_id == user_id,
            VisitorSession.started_at >= start,
            VisitorSession.started_at < end,
        )
        .scalar()
        or 0
    )
    payments = (
        db.query(func.count(DigitalReceipt.id))
        .filter(
            DigitalReceipt.owner_user_id == user_id,
            DigitalReceipt.created_at >= start,
            DigitalReceipt.created_at < end,
        )
        .scalar()
        or 0
    )
    pending_alerts = (
        db.query(func.count(Notification.id))
        .filter(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            Notification.created_at >= start,
            Notification.created_at < end,
        )
        .scalar()
        or 0
    )
    data = {
        "weekStart": start.isoformat(),
        "weekEnd": end.isoformat(),
        "visitors": int(visitors),
        "paymentsMade": int(payments),
        "pendingAlerts": int(pending_alerts),
    }
    key = start.date().isoformat()
    row = (
        db.query(WeeklySummaryLog)
        .filter(WeeklySummaryLog.user_id == user_id, WeeklySummaryLog.week_start_iso == key)
        .first()
    )
    if not row:
        row = WeeklySummaryLog(user_id=user_id, week_start_iso=key, summary_json=json.dumps(data))
        db.add(row)
    else:
        row.summary_json = json.dumps(data)
    db.commit()
    return data
