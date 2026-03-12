import json
import uuid
import re
from datetime import datetime
from decimal import Decimal
from urllib import error, request

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db.models import (
    Estate,
    EstateAlert,
    EstateAlertType,
    EstateMeetingResponse,
    EstatePollVote,
    Home,
    HomeownerPayment,
    HomeownerPaymentStatus,
    HomeownerWallet,
    MeetingResponseType,
    Notification,
    User,
    UserRole,
)
from app.socket.server import sio
from app.services.provider_integrations import send_push_fcm
from app.services.payment_proof_service import save_payment_proof

settings = get_settings()


def _extract_paystack_error(detail: str) -> tuple[str | None, str]:
    fallback_message = (detail or "").strip()
    try:
        parsed = json.loads(detail)
    except Exception:
        if re.search(r"(^|\\D)1010(\\D|$)", fallback_message):
            return "1010", fallback_message
        return None, fallback_message

    code = parsed.get("code")
    message = parsed.get("message") or fallback_message
    if not code and isinstance(parsed.get("data"), dict):
        code = parsed["data"].get("code")
        message = parsed["data"].get("message") or message
    if code is not None:
        code = str(code).strip()
    if not code and re.search(r"(^|\\D)1010(\\D|$)", str(message)):
        code = "1010"
    return code, str(message).strip()


def _to_money(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _serialize_alert(row: EstateAlert) -> dict:
    poll_options = []
    if row.poll_options:
        try:
            poll_options = json.loads(row.poll_options) if row.poll_options else []
        except Exception:
            poll_options = []
    target_ids = []
    if row.target_homeowner_ids:
        try:
            target_ids = json.loads(row.target_homeowner_ids) if row.target_homeowner_ids else []
        except Exception:
            target_ids = []
    return {
        "id": row.id,
        "estateId": row.estate_id,
        "title": row.title,
        "description": row.description or "",
        "alertType": row.alert_type.value if hasattr(row.alert_type, "value") else str(row.alert_type),
        "amountDue": _to_money(row.amount_due) if row.amount_due is not None else None,
        "dueDate": row.due_date.isoformat() if row.due_date else None,
        "pollOptions": poll_options,
        "targetHomeownerIds": target_ids,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def _resolve_estate_or_404(db: Session, estate_id: str) -> Estate:
    estate = db.query(Estate).filter(Estate.id == estate_id).first()
    if not estate:
        raise AppException("Estate not found", status_code=404)
    return estate


def _require_estate_admin_access(db: Session, estate_id: str, user_id: str) -> Estate:
    estate = db.query(Estate).filter(Estate.id == estate_id, Estate.owner_id == user_id).first()
    if not estate:
        raise AppException("Estate not found for this account", status_code=404)
    return estate


def _is_homeowner_in_estate(db: Session, estate_id: str, homeowner_id: str) -> bool:
    row = (
        db.query(Home.id)
        .filter(Home.estate_id == estate_id, Home.homeowner_id == homeowner_id)
        .first()
    )
    return bool(row)


def _homeowner_ids_for_estate(db: Session, estate_id: str) -> list[str]:
    rows = (
        db.query(Home.homeowner_id)
        .filter(Home.estate_id == estate_id, Home.homeowner_id.is_not(None))
        .distinct()
        .all()
    )
    return [row[0] for row in rows if row and row[0]]


def create_estate_alert(
    db: Session,
    *,
    estate_id: str,
    estate_admin_id: str,
    title: str,
    description: str,
    alert_type: str,
    amount_due: float | None,
    due_date: datetime | None,
    poll_options: list[str] | None = None,
    target_homeowner_ids: list[str] | None = None,
) -> dict:
    _require_estate_admin_access(db, estate_id=estate_id, user_id=estate_admin_id)

    clean_title = (title or "").strip()
    clean_description = (description or "").strip()
    if not clean_title:
        raise AppException("title is required", status_code=400)

    normalized_type = (alert_type or "").strip().lower()
    try:
        alert_type_enum = EstateAlertType(normalized_type)
    except ValueError:
        raise AppException("Invalid alert_type", status_code=400)

    normalized_amount = None
    if alert_type_enum == EstateAlertType.payment_request:
        if amount_due is None:
            raise AppException("amount_due is required for payment_request alerts", status_code=400)
        normalized_amount = round(float(amount_due), 2)
        if normalized_amount <= 0:
            raise AppException("amount_due must be greater than 0", status_code=400)
    elif amount_due is not None:
        normalized_amount = round(float(amount_due), 2)

    normalized_poll_options = []
    if alert_type_enum == EstateAlertType.poll:
        if not poll_options or len([opt for opt in poll_options if str(opt).strip()]) < 2:
            raise AppException("poll_options must include at least 2 options", status_code=400)
        normalized_poll_options = [str(opt).strip() for opt in poll_options if str(opt).strip()]

    target_ids = []
    if target_homeowner_ids:
        target_ids = [str(item).strip() for item in target_homeowner_ids if str(item).strip()]
        if target_ids:
            valid_ids = set(_homeowner_ids_for_estate(db, estate_id=estate_id))
            invalid = [hid for hid in target_ids if hid not in valid_ids]
            if invalid:
                raise AppException("One or more homeowners are not linked to this estate", status_code=400)

    alert = EstateAlert(
        estate_id=estate_id,
        title=clean_title,
        description=clean_description,
        alert_type=alert_type_enum,
        amount_due=normalized_amount,
        due_date=due_date,
        poll_options=json.dumps(normalized_poll_options) if normalized_poll_options else "",
        target_homeowner_ids=json.dumps(target_ids) if target_ids else "",
    )
    db.add(alert)
    db.flush()

    homeowner_ids = target_ids or _homeowner_ids_for_estate(db, estate_id=estate_id)
    for homeowner_id in homeowner_ids:
        push_payload = {
            "message": clean_title,
            "alertId": alert.id,
            "estateId": estate_id,
            "alertType": alert_type_enum.value,
            "amountDue": normalized_amount,
            "dueDate": due_date.isoformat() if due_date else None,
        }
        db.add(
            Notification(
                user_id=homeowner_id,
                kind="estate.alert",
                payload=json.dumps(push_payload),
            )
        )
        try:
            send_push_fcm(
                db,
                user_id=homeowner_id,
                title="Estate Alert",
                body=clean_title,
                data={
                    "kind": "estate.alert",
                    "alertId": alert.id,
                    "estateId": estate_id,
                    "alertType": alert_type_enum.value,
                },
            )
        except Exception:
            pass
    db.commit()
    db.refresh(alert)

    payload = _serialize_alert(alert)
    payload["status"] = "created"
    payload["paymentSummary"] = {"paid": 0, "pending": len(homeowner_ids), "failed": 0}
    sio.start_background_task(
        sio.emit,
        "ALERT_CREATED",
        payload,
        room=f"estate:{estate_id}:alerts",
        namespace=settings.DASHBOARD_NAMESPACE,
    )
    return payload


def list_estate_alerts(
    db: Session,
    *,
    estate_id: str,
    actor_id: str,
    actor_role: UserRole,
    alert_type: str | None = None,
) -> list[dict]:
    if actor_role == UserRole.estate:
        _require_estate_admin_access(db, estate_id=estate_id, user_id=actor_id)
    elif actor_role == UserRole.homeowner:
        if not _is_homeowner_in_estate(db, estate_id=estate_id, homeowner_id=actor_id):
            raise AppException("You are not linked to this estate", status_code=403)
    else:
        raise AppException("Insufficient permissions", status_code=403)

    q = db.query(EstateAlert).filter(EstateAlert.estate_id == estate_id)
    if alert_type:
        normalized_type = alert_type.strip().lower()
        try:
            alert_type_enum = EstateAlertType(normalized_type)
        except ValueError:
            raise AppException("Invalid alert_type filter", status_code=400)
        q = q.filter(EstateAlert.alert_type == alert_type_enum)
    alerts = q.order_by(EstateAlert.created_at.desc()).all()
    if not alerts:
        return []

    if actor_role == UserRole.homeowner:
        filtered = []
        for row in alerts:
            if not row.target_homeowner_ids:
                filtered.append(row)
                continue
            try:
                target_ids = json.loads(row.target_homeowner_ids) or []
            except Exception:
                target_ids = []
            if not target_ids or actor_id in target_ids:
                filtered.append(row)
        alerts = filtered
        if not alerts:
            return []

    alert_ids = [row.id for row in alerts]
    meeting_counts: dict[str, dict[str, int]] = {
        alert_id: {"attending": 0, "not_attending": 0, "maybe": 0}
        for alert_id in alert_ids
    }
    poll_counts: dict[str, dict[int, int]] = {alert_id: {} for alert_id in alert_ids}

    meeting_rows = (
        db.query(EstateMeetingResponse.estate_alert_id, EstateMeetingResponse.response, func.count(EstateMeetingResponse.id))
        .filter(EstateMeetingResponse.estate_alert_id.in_(alert_ids))
        .group_by(EstateMeetingResponse.estate_alert_id, EstateMeetingResponse.response)
        .all()
    )
    for alert_id, response, count in meeting_rows:
        normalized = response.value if hasattr(response, "value") else str(response)
        if alert_id in meeting_counts and normalized in meeting_counts[alert_id]:
            meeting_counts[alert_id][normalized] = int(count or 0)

    poll_rows = (
        db.query(EstatePollVote.estate_alert_id, EstatePollVote.option_index, func.count(EstatePollVote.id))
        .filter(EstatePollVote.estate_alert_id.in_(alert_ids))
        .group_by(EstatePollVote.estate_alert_id, EstatePollVote.option_index)
        .all()
    )
    for alert_id, option_index, count in poll_rows:
        poll_counts.setdefault(alert_id, {})[int(option_index)] = int(count or 0)
    aggregates = (
        db.query(
            HomeownerPayment.estate_alert_id,
            HomeownerPayment.status,
            func.count(HomeownerPayment.id),
        )
        .filter(HomeownerPayment.estate_alert_id.in_(alert_ids))
        .group_by(HomeownerPayment.estate_alert_id, HomeownerPayment.status)
        .all()
    )
    summary_map: dict[str, dict[str, int]] = {
        alert_id: {"paid": 0, "pending": 0, "failed": 0}
        for alert_id in alert_ids
    }
    for alert_id, status, count in aggregates:
        normalized_status = status.value if hasattr(status, "value") else str(status)
        if normalized_status in summary_map[alert_id]:
            summary_map[alert_id][normalized_status] = int(count or 0)

    my_payment_map: dict[str, HomeownerPayment] = {}
    my_meeting_map: dict[str, EstateMeetingResponse] = {}
    my_poll_map: dict[str, EstatePollVote] = {}
    if actor_role == UserRole.homeowner:
        my_rows = (
            db.query(HomeownerPayment)
            .filter(
                HomeownerPayment.estate_alert_id.in_(alert_ids),
                HomeownerPayment.homeowner_id == actor_id,
            )
            .all()
        )
        my_payment_map = {row.estate_alert_id: row for row in my_rows}
        my_meeting_rows = (
            db.query(EstateMeetingResponse)
            .filter(
                EstateMeetingResponse.estate_alert_id.in_(alert_ids),
                EstateMeetingResponse.homeowner_id == actor_id,
            )
            .all()
        )
        my_meeting_map = {row.estate_alert_id: row for row in my_meeting_rows}
        my_poll_rows = (
            db.query(EstatePollVote)
            .filter(
                EstatePollVote.estate_alert_id.in_(alert_ids),
                EstatePollVote.homeowner_id == actor_id,
            )
            .all()
        )
        my_poll_map = {row.estate_alert_id: row for row in my_poll_rows}

    response: list[dict] = []
    for row in alerts:
        data = _serialize_alert(row)
        data["paymentSummary"] = summary_map.get(row.id, {"paid": 0, "pending": 0, "failed": 0})
        if actor_role == UserRole.homeowner:
            mine = my_payment_map.get(row.id)
            data["myPayment"] = {
                "status": (mine.status.value if mine and hasattr(mine.status, "value") else (mine.status if mine else "pending")),
                "amountPaid": _to_money(mine.amount_paid) if mine else 0.0,
                "reference": mine.payment_provider_reference if mine else None,
                "paymentMethod": mine.payment_method if mine else None,
                "note": mine.payment_note if mine else None,
                "proofUrl": mine.payment_proof_url if mine else None,
                "receiptUrl": mine.receipt_url if mine else None,
                "paidAt": mine.paid_at.isoformat() if mine and mine.paid_at else None,
            }
            my_meeting = my_meeting_map.get(row.id)
            data["myMeetingResponse"] = (
                my_meeting.response.value if my_meeting and hasattr(my_meeting.response, "value") else (my_meeting.response if my_meeting else None)
            )
            my_poll = my_poll_map.get(row.id)
            data["myPollVote"] = int(my_poll.option_index) if my_poll else None
        if row.alert_type == EstateAlertType.meeting:
            data["meetingResponses"] = meeting_counts.get(row.id, {"attending": 0, "not_attending": 0, "maybe": 0})
        if row.alert_type == EstateAlertType.poll:
            counts = poll_counts.get(row.id, {})
            total_votes = sum(counts.values())
            options = data.get("pollOptions") or []
            data["pollResults"] = [
                {
                    "option": options[idx] if idx < len(options) else f"Option {idx + 1}",
                    "count": counts.get(idx, 0),
                    "percent": round((counts.get(idx, 0) / total_votes) * 100, 1) if total_votes else 0,
                    "index": idx,
                }
                for idx in range(len(options))
            ]
        response.append(data)
    return response


def record_meeting_response(
    db: Session,
    *,
    alert_id: str,
    homeowner_id: str,
    response: str,
) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert or alert.alert_type != EstateAlertType.meeting:
        raise AppException("Meeting alert not found", status_code=404)
    if not _is_homeowner_in_estate(db, estate_id=alert.estate_id, homeowner_id=homeowner_id):
        raise AppException("You are not linked to this estate", status_code=403)
    try:
        response_enum = MeetingResponseType(response)
    except ValueError:
        raise AppException("Invalid meeting response", status_code=400)

    row = (
        db.query(EstateMeetingResponse)
        .filter(EstateMeetingResponse.estate_alert_id == alert_id, EstateMeetingResponse.homeowner_id == homeowner_id)
        .first()
    )
    if row:
        row.response = response_enum
    else:
        row = EstateMeetingResponse(
            estate_alert_id=alert_id,
            homeowner_id=homeowner_id,
            response=response_enum,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"alertId": alert_id, "response": row.response.value}


def record_poll_vote(
    db: Session,
    *,
    alert_id: str,
    homeowner_id: str,
    option_index: int,
) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert or alert.alert_type != EstateAlertType.poll:
        raise AppException("Poll not found", status_code=404)
    if not _is_homeowner_in_estate(db, estate_id=alert.estate_id, homeowner_id=homeowner_id):
        raise AppException("You are not linked to this estate", status_code=403)
    options = []
    if alert.poll_options:
        try:
            options = json.loads(alert.poll_options)
        except Exception:
            options = []
    if option_index < 0 or option_index >= len(options):
        raise AppException("Invalid poll option", status_code=400)

    row = (
        db.query(EstatePollVote)
        .filter(EstatePollVote.estate_alert_id == alert_id, EstatePollVote.homeowner_id == homeowner_id)
        .first()
    )
    if row:
        row.option_index = int(option_index)
    else:
        row = EstatePollVote(
            estate_alert_id=alert_id,
            homeowner_id=homeowner_id,
            option_index=int(option_index),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"alertId": alert_id, "optionIndex": int(row.option_index)}


def update_estate_alert(
    db: Session,
    *,
    alert_id: str,
    estate_admin_id: str,
    title: str,
    description: str,
    target_homeowner_ids: list[str] | None = None,
) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert:
        raise AppException("Alert not found", status_code=404)
    _require_estate_admin_access(db, estate_id=alert.estate_id, user_id=estate_admin_id)
    if alert.alert_type != EstateAlertType.notice:
        raise AppException("Only broadcast alerts can be edited", status_code=400)

    clean_title = (title or "").strip()
    if not clean_title:
        raise AppException("title is required", status_code=400)
    clean_description = (description or "").strip()

    target_ids = []
    if target_homeowner_ids:
        target_ids = [str(item).strip() for item in target_homeowner_ids if str(item).strip()]
        if target_ids:
            valid_ids = set(_homeowner_ids_for_estate(db, estate_id=alert.estate_id))
            invalid = [hid for hid in target_ids if hid not in valid_ids]
            if invalid:
                raise AppException("One or more homeowners are not linked to this estate", status_code=400)

    alert.title = clean_title
    alert.description = clean_description
    alert.target_homeowner_ids = json.dumps(target_ids) if target_ids else ""
    db.commit()
    db.refresh(alert)
    return _serialize_alert(alert)


def delete_estate_alert(
    db: Session,
    *,
    alert_id: str,
    estate_admin_id: str,
) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert:
        raise AppException("Alert not found", status_code=404)
    _require_estate_admin_access(db, estate_id=alert.estate_id, user_id=estate_admin_id)
    if alert.alert_type != EstateAlertType.notice:
        raise AppException("Only broadcast alerts can be deleted", status_code=400)

    db.query(HomeownerPayment).filter(HomeownerPayment.estate_alert_id == alert_id).delete(synchronize_session=False)
    db.query(EstateMeetingResponse).filter(EstateMeetingResponse.estate_alert_id == alert_id).delete(synchronize_session=False)
    db.query(EstatePollVote).filter(EstatePollVote.estate_alert_id == alert_id).delete(synchronize_session=False)
    db.query(Notification).filter(
        Notification.kind == "estate.alert",
        Notification.payload.like(f"%{alert_id}%"),
    ).delete(synchronize_session=False)
    db.delete(alert)
    db.commit()
    return {"deleted": True, "alertId": alert_id}


def create_homeowner_maintenance_request(
    db: Session,
    *,
    homeowner_id: str,
    title: str,
    description: str,
) -> dict:
    estate_row = (
        db.query(Home.estate_id)
        .filter(Home.homeowner_id == homeowner_id, Home.estate_id.is_not(None))
        .order_by(Home.created_at.desc())
        .first()
    )
    if not estate_row or not estate_row[0]:
        raise AppException("You are not linked to any estate", status_code=404)
    estate = _resolve_estate_or_404(db, estate_id=estate_row[0])
    clean_title = (title or "").strip()
    if not clean_title:
        raise AppException("title is required", status_code=400)
    clean_description = (description or "").strip()

    alert = EstateAlert(
        estate_id=estate.id,
        title=clean_title,
        description=clean_description,
        alert_type=EstateAlertType.maintenance_request,
    )
    db.add(alert)
    db.flush()

    db.add(
        Notification(
            user_id=estate.owner_id,
            kind="estate.maintenance",
            payload=json.dumps(
                {
                    "message": f"New maintenance request: {clean_title}",
                    "alertId": alert.id,
                    "estateId": estate.id,
                }
            ),
        )
    )
    db.commit()
    db.refresh(alert)
    payload = _serialize_alert(alert)
    payload["status"] = "created"
    return payload


def list_homeowner_alerts(db: Session, *, homeowner_id: str) -> list[dict]:
    estate_row = (
        db.query(Home.estate_id)
        .filter(Home.homeowner_id == homeowner_id, Home.estate_id.is_not(None))
        .order_by(Home.created_at.desc())
        .first()
    )
    if not estate_row or not estate_row[0]:
        return []
    return list_estate_alerts(
        db,
        estate_id=estate_row[0],
        actor_id=homeowner_id,
        actor_role=UserRole.homeowner,
    )


def _resolve_or_create_payment_row(
    db: Session,
    *,
    alert_id: str,
    homeowner_id: str,
) -> HomeownerPayment:
    row = (
        db.query(HomeownerPayment)
        .filter(
            HomeownerPayment.estate_alert_id == alert_id,
            HomeownerPayment.homeowner_id == homeowner_id,
        )
        .first()
    )
    if row:
        return row
    row = HomeownerPayment(
        estate_alert_id=alert_id,
        homeowner_id=homeowner_id,
        status=HomeownerPaymentStatus.pending,
        amount_paid=0,
    )
    db.add(row)
    db.flush()
    return row


def _resolve_or_create_wallet(db: Session, *, homeowner_id: str) -> HomeownerWallet:
    wallet = (
        db.query(HomeownerWallet)
        .filter(HomeownerWallet.user_id == homeowner_id)
        .with_for_update()
        .first()
    )
    if wallet:
        return wallet
    wallet = HomeownerWallet(user_id=homeowner_id, balance=0, currency="NGN")
    db.add(wallet)
    db.flush()
    return wallet


def _notify_payment_status(
    db: Session,
    *,
    alert: EstateAlert,
    homeowner_id: str,
    status: str,
    receipt_url: str | None,
    reference: str | None,
    message: str,
) -> None:
    db.add(
        Notification(
            user_id=homeowner_id,
            kind="estate.payment.status",
            payload=json.dumps(
                {
                    "message": message,
                    "alertId": alert.id,
                    "status": status,
                    "receiptUrl": receipt_url,
                    "reference": reference,
                }
            ),
        )
    )

    estate = _resolve_estate_or_404(db, alert.estate_id)
    db.add(
        Notification(
            user_id=estate.owner_id,
            kind="estate.payment.status",
            payload=json.dumps(
                {
                    "message": message,
                    "alertId": alert.id,
                    "status": status,
                    "homeownerId": homeowner_id,
                    "reference": reference,
                }
            ),
        )
    )


def initialize_alert_payment(
    db: Session,
    *,
    alert_id: str,
    homeowner_id: str,
    payment_method: str = "paystack",
    reference: str | None = None,
    callback_url: str | None = None,
) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert:
        raise AppException("Alert not found", status_code=404)
    if alert.alert_type != EstateAlertType.payment_request:
        raise AppException("This alert is not payable", status_code=400)
    if not _is_homeowner_in_estate(db, estate_id=alert.estate_id, homeowner_id=homeowner_id):
        raise AppException("You are not linked to this estate", status_code=403)
    if not alert.amount_due or _to_money(alert.amount_due) <= 0:
        raise AppException("Invalid payment amount", status_code=400)

    homeowner = db.query(User).filter(User.id == homeowner_id).first()
    if not homeowner:
        raise AppException("Homeowner not found", status_code=404)

    payment = _resolve_or_create_payment_row(db, alert_id=alert_id, homeowner_id=homeowner_id)
    if payment.status == HomeownerPaymentStatus.paid:
        return {
            "status": "paid",
            "receiptUrl": payment.receipt_url,
            "reference": payment.payment_provider_reference,
            "paymentMethod": payment.payment_method or "paystack",
        }

    normalized_method = (payment_method or "paystack").strip().lower()
    if normalized_method == "paystack":
        if not settings.PAYSTACK_SECRET_KEY:
            raise AppException("Paystack is not configured", status_code=500)

        reference = f"qring-alert-{uuid.uuid4().hex[:18]}"
        payment.status = HomeownerPaymentStatus.pending
        payment.payment_method = "paystack"
        payment.payment_provider_reference = reference
        payment.payment_note = None

        payload = {
            "email": homeowner.email,
            "amount": int(round(_to_money(alert.amount_due) * 100)),
            "currency": "NGN",
            "reference": reference,
            "callback_url": callback_url or f"{settings.FRONTEND_BASE_URL.rstrip('/')}/dashboard/homeowner/alerts",
            "metadata": {
                "payment_kind": "estate_alert",
                "estate_alert_id": alert.id,
                "estate_id": alert.estate_id,
                "homeowner_id": homeowner.id,
                "source": "qring-estate-alerts",
            },
        }

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            "https://api.paystack.co/transaction/initialize",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            error_code, error_message = _extract_paystack_error(detail)
            if error_code == "1010":
                raise AppException(
                    "Paystack blocked initialization (1010). "
                    "Check live-mode restrictions: callback/domain allowlist and API IP allowlist.",
                    status_code=502,
                )
            raise AppException(f"Paystack initialize failed: {error_message or detail}", status_code=502)
        except Exception:
            raise AppException("Paystack initialize failed", status_code=502)

        if not data.get("status") or not data.get("data", {}).get("authorization_url"):
            raise AppException("Unable to initialize payment", status_code=502)

        db.commit()
        db.refresh(payment)
        return {
            "status": "pending",
            "reference": payment.payment_provider_reference,
            "authorizationUrl": data["data"]["authorization_url"],
            "accessCode": data["data"].get("access_code"),
            "paymentMethod": "paystack",
        }

    if normalized_method == "bank_transfer":
        clean_ref = (reference or "").strip()
        if not clean_ref or len(clean_ref) < 4:
            raise AppException("Bank transfer reference is required", status_code=400)
        payment.status = HomeownerPaymentStatus.pending
        payment.payment_method = "bank_transfer"
        payment.payment_provider_reference = clean_ref
        payment.payment_note = "Bank transfer reference submitted"
        db.commit()
        db.refresh(payment)

        _notify_payment_status(
            db,
            alert=alert,
            homeowner_id=homeowner_id,
            status="pending",
            receipt_url=None,
            reference=clean_ref,
            message=f"Bank transfer reference submitted for alert: {alert.title}",
        )
        db.commit()
        return {
            "status": "pending",
            "reference": clean_ref,
            "paymentMethod": "bank_transfer",
            "message": "Bank transfer reference submitted for verification.",
        }

    if normalized_method == "wallet":
        amount_due = _to_money(alert.amount_due)
        wallet = _resolve_or_create_wallet(db, homeowner_id=homeowner_id)
        if _to_money(wallet.balance) < amount_due:
            raise AppException("Insufficient wallet balance", status_code=400)

        reference = f"wallet-{uuid.uuid4().hex[:12]}"
        wallet.balance = round(_to_money(wallet.balance) - amount_due, 2)
        payment.status = HomeownerPaymentStatus.paid
        payment.payment_method = "wallet"
        payment.payment_provider_reference = reference
        payment.payment_note = "Wallet payment completed"
        payment.amount_paid = amount_due
        payment.paid_at = datetime.utcnow()
        payment.receipt_url = _build_receipt_url(reference, None)
        db.commit()
        db.refresh(payment)

        _notify_payment_status(
            db,
            alert=alert,
            homeowner_id=homeowner_id,
            status="paid",
            receipt_url=payment.receipt_url,
            reference=reference,
            message=f"Wallet payment completed for alert: {alert.title}",
        )
        db.commit()
        return {
            "status": "paid",
            "reference": reference,
            "paymentMethod": "wallet",
            "message": "Wallet payment completed.",
            "receiptUrl": payment.receipt_url,
        }

    raise AppException("Unsupported payment method", status_code=400)


def _build_receipt_url(reference: str, paystack_transaction_id: int | str | None) -> str:
    if paystack_transaction_id:
        return f"https://dashboard.paystack.com/#/transactions/{paystack_transaction_id}"
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/dashboard/homeowner/alerts?receipt={reference}"


def apply_alert_payment_webhook(
    db: Session,
    *,
    metadata: dict,
    reference: str | None,
    status: str,
    amount_kobo: int | None,
    paid_at_iso: str | None,
    paystack_transaction_id: int | str | None,
) -> dict:
    if metadata.get("payment_kind") != "estate_alert":
        return {"status": "ignored"}

    alert_id = metadata.get("estate_alert_id")
    homeowner_id = metadata.get("homeowner_id")
    if not alert_id or not homeowner_id:
        return {"status": "ignored"}

    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert:
        return {"status": "ignored"}

    payment = (
        db.query(HomeownerPayment)
        .filter(
            and_(
                HomeownerPayment.estate_alert_id == alert_id,
                HomeownerPayment.homeowner_id == homeowner_id,
            )
        )
        .first()
    )
    if not payment:
        payment = HomeownerPayment(
            estate_alert_id=alert_id,
            homeowner_id=homeowner_id,
            amount_paid=0,
            status=HomeownerPaymentStatus.pending,
        )
        db.add(payment)
        db.flush()

    normalized_status = (status or "").strip().lower()
    if normalized_status == "success":
        payment.status = HomeownerPaymentStatus.paid
        payment.payment_method = "paystack"
        payment.amount_paid = round(((amount_kobo or 0) / 100), 2)
        payment.payment_provider_reference = reference or payment.payment_provider_reference
        payment.receipt_url = _build_receipt_url(reference or "", paystack_transaction_id)
        if paid_at_iso:
            try:
                payment.paid_at = datetime.fromisoformat(paid_at_iso.replace("Z", "+00:00"))
            except Exception:
                payment.paid_at = datetime.utcnow()
        else:
            payment.paid_at = datetime.utcnow()
    elif normalized_status in {"failed", "abandoned"}:
        payment.status = HomeownerPaymentStatus.failed
        payment.payment_provider_reference = reference or payment.payment_provider_reference
    else:
        return {"status": "ignored"}

    db.add(
        Notification(
            user_id=homeowner_id,
            kind="estate.payment.status",
            payload=json.dumps(
                {
                    "message": f"Payment {payment.status.value} for alert: {alert.title}",
                    "alertId": alert.id,
                    "status": payment.status.value,
                    "receiptUrl": payment.receipt_url,
                }
            ),
        )
    )

    estate = _resolve_estate_or_404(db, alert.estate_id)
    db.add(
        Notification(
            user_id=estate.owner_id,
            kind="estate.payment.status",
            payload=json.dumps(
                {
                    "message": f"Payment {payment.status.value} from homeowner for alert: {alert.title}",
                    "alertId": alert.id,
                    "status": payment.status.value,
                    "homeownerId": homeowner_id,
                }
            ),
        )
    )

    db.commit()
    db.refresh(payment)

    payload = {
        "alertId": alert.id,
        "estateId": alert.estate_id,
        "status": payment.status.value,
        "homeownerId": homeowner_id,
        "amountPaid": _to_money(payment.amount_paid),
        "receiptUrl": payment.receipt_url,
        "paidAt": payment.paid_at.isoformat() if payment.paid_at else None,
    }
    sio.start_background_task(
        sio.emit,
        "PAYMENT_STATUS_UPDATED",
        payload,
        room=f"estate:{alert.estate_id}:alerts",
        namespace=settings.DASHBOARD_NAMESPACE,
    )
    return {"status": "processed", **payload}


def list_estate_alert_payment_overview(db: Session, *, estate_id: str, estate_admin_id: str) -> list[dict]:
    _require_estate_admin_access(db, estate_id=estate_id, user_id=estate_admin_id)
    alerts = (
        db.query(EstateAlert)
        .filter(EstateAlert.estate_id == estate_id)
        .order_by(EstateAlert.created_at.desc())
        .all()
    )
    if not alerts:
        return []

    homeowner_ids = _homeowner_ids_for_estate(db, estate_id=estate_id)
    homeowners = (
        db.query(User).filter(User.id.in_(homeowner_ids)).all()
        if homeowner_ids
        else []
    )
    homeowner_map = {row.id: row for row in homeowners}

    alert_ids = [row.id for row in alerts]
    payments = (
        db.query(HomeownerPayment)
        .filter(HomeownerPayment.estate_alert_id.in_(alert_ids))
        .all()
    )
    by_alert: dict[str, list[HomeownerPayment]] = {}
    for row in payments:
        by_alert.setdefault(row.estate_alert_id, []).append(row)

    result: list[dict] = []
    for alert in alerts:
        payment_by_homeowner = {row.homeowner_id: row for row in by_alert.get(alert.id, [])}
        rows = []
        for homeowner_id in homeowner_ids:
            homeowner = homeowner_map.get(homeowner_id)
            payment = payment_by_homeowner.get(homeowner_id)
            rows.append(
                {
                    "homeownerId": homeowner_id,
                    "homeownerName": homeowner.full_name if homeowner else "Homeowner",
                    "homeownerEmail": homeowner.email if homeowner else "",
                    "status": (
                        payment.status.value
                        if payment and hasattr(payment.status, "value")
                        else (payment.status if payment else "pending")
                    ),
                    "amountPaid": _to_money(payment.amount_paid) if payment else 0.0,
                    "reference": payment.payment_provider_reference if payment else None,
                    "paymentMethod": payment.payment_method if payment else None,
                    "note": payment.payment_note if payment else None,
                    "proofUrl": payment.payment_proof_url if payment else None,
                    "receiptUrl": payment.receipt_url if payment else None,
                    "paidAt": payment.paid_at.isoformat() if payment and payment.paid_at else None,
                }
            )
        result.append(
            {
                **_serialize_alert(alert),
                "homeowners": rows,
            }
        )
    return result


def send_payment_reminders(db: Session, *, alert_id: str, estate_admin_id: str) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert or alert.alert_type != EstateAlertType.payment_request:
        raise AppException("Payment request not found", status_code=404)
    estate = _require_estate_admin_access(db, estate_id=alert.estate_id, user_id=estate_admin_id)

    homeowner_ids = _homeowner_ids_for_estate(db, estate_id=estate.id)
    if not homeowner_ids:
        return {"status": "ok", "reminded": 0}

    payments = (
        db.query(HomeownerPayment)
        .filter(HomeownerPayment.estate_alert_id == alert.id)
        .all()
    )
    by_homeowner = {row.homeowner_id: row for row in payments}

    reminded = 0
    for homeowner_id in homeowner_ids:
        payment = by_homeowner.get(homeowner_id)
        status = payment.status.value if payment and hasattr(payment.status, "value") else (payment.status if payment else "pending")
        if status == "paid":
            continue
        if payment:
            payment.reminder_sent_at = datetime.utcnow()
        db.add(
            Notification(
                user_id=homeowner_id,
                kind="estate.payment.reminder",
                payload=json.dumps(
                    {
                        "message": f"Payment reminder: {alert.title}",
                        "alertId": alert.id,
                        "estateId": alert.estate_id,
                    }
                ),
            )
        )
        try:
            send_push_fcm(
                db,
                user_id=homeowner_id,
                title="Payment Reminder",
                body=f"Payment reminder: {alert.title}",
                data={
                    "kind": "estate.payment.reminder",
                    "alertId": alert.id,
                    "estateId": alert.estate_id,
                },
            )
        except Exception:
            pass
        reminded += 1

    db.commit()
    return {"status": "ok", "reminded": reminded}


def verify_estate_alert_payment(
    db: Session,
    *,
    alert_id: str,
    estate_admin_id: str,
    homeowner_id: str,
    payment_method: str | None = None,
    reference: str | None = None,
    receipt_url: str | None = None,
) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert or alert.alert_type != EstateAlertType.payment_request:
        raise AppException("Payment request not found", status_code=404)
    _require_estate_admin_access(db, estate_id=alert.estate_id, user_id=estate_admin_id)
    if not _is_homeowner_in_estate(db, estate_id=alert.estate_id, homeowner_id=homeowner_id):
        raise AppException("Homeowner not linked to this estate", status_code=404)

    payment = _resolve_or_create_payment_row(db, alert_id=alert_id, homeowner_id=homeowner_id)
    amount_due = _to_money(alert.amount_due)
    if amount_due <= 0:
        raise AppException("Invalid payment amount", status_code=400)

    normalized_method = (payment_method or payment.payment_method or "bank_transfer").strip().lower()
    payment.status = HomeownerPaymentStatus.paid
    payment.payment_method = normalized_method
    if reference:
        payment.payment_provider_reference = reference.strip()
    if receipt_url:
        payment.receipt_url = receipt_url
    if not payment.receipt_url:
        payment.receipt_url = _build_receipt_url(payment.payment_provider_reference or "", None)
    payment.amount_paid = amount_due
    payment.paid_at = datetime.utcnow()
    payment.payment_note = "Verified by estate admin"

    db.commit()
    db.refresh(payment)

    _notify_payment_status(
        db,
        alert=alert,
        homeowner_id=homeowner_id,
        status="paid",
        receipt_url=payment.receipt_url,
        reference=payment.payment_provider_reference,
        message=f"Payment verified for alert: {alert.title}",
    )
    db.commit()

    return {
        "status": "paid",
        "reference": payment.payment_provider_reference,
        "paymentMethod": payment.payment_method,
        "receiptUrl": payment.receipt_url,
        "paidAt": payment.paid_at.isoformat() if payment.paid_at else None,
    }


def attach_alert_payment_proof(
    db: Session,
    *,
    alert_id: str,
    homeowner_id: str,
    media_bytes: bytes,
    filename_hint: str,
    content_type: str | None,
) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert or alert.alert_type != EstateAlertType.payment_request:
        raise AppException("Payment request not found", status_code=404)
    if not _is_homeowner_in_estate(db, estate_id=alert.estate_id, homeowner_id=homeowner_id):
        raise AppException("You are not linked to this estate", status_code=403)

    proof = save_payment_proof(
        media_bytes=media_bytes,
        filename_hint=filename_hint,
        content_type=content_type,
        alert_id=alert_id,
        homeowner_id=homeowner_id,
    )

    payment = _resolve_or_create_payment_row(db, alert_id=alert_id, homeowner_id=homeowner_id)
    payment.payment_proof_url = proof["url"]
    if not payment.payment_method:
        payment.payment_method = "bank_transfer"
    if payment.status != HomeownerPaymentStatus.paid:
        payment.status = HomeownerPaymentStatus.pending
    payment.payment_note = "Payment proof uploaded"
    db.commit()
    db.refresh(payment)

    _notify_payment_status(
        db,
        alert=alert,
        homeowner_id=homeowner_id,
        status=payment.status.value if hasattr(payment.status, "value") else str(payment.status),
        receipt_url=payment.receipt_url,
        reference=payment.payment_provider_reference,
        message=f"Payment proof uploaded for alert: {alert.title}",
    )
    db.commit()

    return {
        "proofUrl": proof["url"],
        "status": payment.status.value if hasattr(payment.status, "value") else str(payment.status),
    }


def run_scheduled_payment_reminders(db: Session) -> dict:
    now = datetime.utcnow()
    alerts = (
        db.query(EstateAlert)
        .filter(EstateAlert.alert_type == EstateAlertType.payment_request)
        .order_by(EstateAlert.created_at.desc())
        .all()
    )
    if not alerts:
        return {"status": "ok", "reminded": 0}

    estate_ids = {row.estate_id for row in alerts if row.estate_id}
    estates = db.query(Estate).filter(Estate.id.in_(estate_ids)).all() if estate_ids else []
    reminder_frequency_by_estate = {
        row.id: max(int(row.reminder_frequency_days or 1), 1) for row in estates
    }

    reminded = 0
    for alert in alerts:
        if alert.due_date and alert.due_date > now:
            continue
        if not alert.due_date and (now - alert.created_at).days < 3:
            continue

        frequency_days = reminder_frequency_by_estate.get(alert.estate_id, 1)
        homeowner_ids = _homeowner_ids_for_estate(db, estate_id=alert.estate_id)
        if not homeowner_ids:
            continue

        payments = (
            db.query(HomeownerPayment)
            .filter(HomeownerPayment.estate_alert_id == alert.id)
            .all()
        )
        by_homeowner = {row.homeowner_id: row for row in payments}

        for homeowner_id in homeowner_ids:
            payment = by_homeowner.get(homeowner_id)
            status = payment.status.value if payment and hasattr(payment.status, "value") else (payment.status if payment else "pending")
            if status == "paid":
                continue
            if payment and payment.reminder_sent_at and (now - payment.reminder_sent_at).days < frequency_days:
                continue
            if payment:
                payment.reminder_sent_at = now

            db.add(
                Notification(
                    user_id=homeowner_id,
                    kind="estate.payment.reminder",
                    payload=json.dumps(
                        {
                            "message": f"Payment reminder: {alert.title}",
                            "alertId": alert.id,
                            "estateId": alert.estate_id,
                        }
                    ),
                )
            )
            try:
                send_push_fcm(
                    db,
                    user_id=homeowner_id,
                    title="Payment Reminder",
                    body=f"Payment reminder: {alert.title}",
                    data={
                        "kind": "estate.payment.reminder",
                        "alertId": alert.id,
                        "estateId": alert.estate_id,
                    },
                )
            except Exception:
                pass
            reminded += 1

    db.commit()
    return {"status": "ok", "reminded": reminded}
