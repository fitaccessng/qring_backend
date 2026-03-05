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
    Home,
    HomeownerPayment,
    HomeownerPaymentStatus,
    Notification,
    User,
    UserRole,
)
from app.socket.server import sio

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
    return {
        "id": row.id,
        "estateId": row.estate_id,
        "title": row.title,
        "description": row.description or "",
        "alertType": row.alert_type.value if hasattr(row.alert_type, "value") else str(row.alert_type),
        "amountDue": _to_money(row.amount_due) if row.amount_due is not None else None,
        "dueDate": row.due_date.isoformat() if row.due_date else None,
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

    alert = EstateAlert(
        estate_id=estate_id,
        title=clean_title,
        description=clean_description,
        alert_type=alert_type_enum,
        amount_due=normalized_amount,
        due_date=due_date,
    )
    db.add(alert)
    db.flush()

    homeowner_ids = _homeowner_ids_for_estate(db, estate_id=estate_id)
    for homeowner_id in homeowner_ids:
        db.add(
            Notification(
                user_id=homeowner_id,
                kind="estate.alert",
                payload=json.dumps(
                    {
                        "message": clean_title,
                        "alertId": alert.id,
                        "estateId": estate_id,
                        "alertType": alert_type_enum.value,
                        "amountDue": normalized_amount,
                        "dueDate": due_date.isoformat() if due_date else None,
                    }
                ),
            )
        )
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

    alert_ids = [row.id for row in alerts]
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
                "receiptUrl": mine.receipt_url if mine else None,
                "paidAt": mine.paid_at.isoformat() if mine and mine.paid_at else None,
            }
        response.append(data)
    return response


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


def initialize_alert_payment(
    db: Session,
    *,
    alert_id: str,
    homeowner_id: str,
    callback_url: str | None = None,
) -> dict:
    alert = db.query(EstateAlert).filter(EstateAlert.id == alert_id).first()
    if not alert:
        raise AppException("Alert not found", status_code=404)
    if alert.alert_type != EstateAlertType.payment_request:
        raise AppException("This alert is not payable", status_code=400)
    if not _is_homeowner_in_estate(db, estate_id=alert.estate_id, homeowner_id=homeowner_id):
        raise AppException("You are not linked to this estate", status_code=403)
    if not settings.PAYSTACK_SECRET_KEY:
        raise AppException("Paystack is not configured", status_code=500)
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
        }

    reference = f"qring-alert-{uuid.uuid4().hex[:18]}"
    payment.status = HomeownerPaymentStatus.pending
    payment.payment_provider_reference = reference

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
    }


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
