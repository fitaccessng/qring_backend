from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable

from anyio import from_thread
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.time import utc_now
from app.db.base import Base
from app.db.models import (
    AlertDeliveryStatus,
    AuditLog,
    EmergencyAlert,
    EmergencyAlertEvent,
    EmergencyAlertPriority,
    EmergencyAlertStatus,
    EmergencyAlertType,
    Estate,
    Home,
    HomeownerSetting,
    User,
    UserRole,
    VisitorReport,
    VisitorReportSeverity,
    VisitorReportStatus,
    VisitorSession,
    WatchlistEntry,
    WatchlistRiskLevel,
)
from app.services.notification_service import create_notification
from app.socket.server import sio

settings = get_settings()
MAX_VISITOR_REPORTS_PER_DAY = 5

PRIORITY_BY_TYPE = {
    EmergencyAlertType.panic: EmergencyAlertPriority.critical,
    EmergencyAlertType.fire: EmergencyAlertPriority.critical,
    EmergencyAlertType.break_in: EmergencyAlertPriority.high,
}

SECURITY_MESSAGE_BY_TYPE = {
    EmergencyAlertType.panic: "Immediate distress alert. Treat as potential threat or kidnapping scenario.",
    EmergencyAlertType.fire: "Fire alert. Coordinate nearest responders and evacuation support.",
    EmergencyAlertType.break_in: "Break-in alert. Dispatch guards and secure access points.",
}
def create_safety_tables(bind) -> None:
    for table in [
        "emergency_alerts",
        "emergency_alert_events",
        "visitor_reports",
        "watchlist_entries",
    ]:
        Base.metadata.tables[table].create(bind=bind, checkfirst=True)


def _to_float(value: Decimal | float | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _json_dumps(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=True)


def _json_loads(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _normalize_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _normalize_phone(value: str | None) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits[-11:] if digits else ""


def _log_audit(db: Session, *, actor_user_id: str | None, action: str, resource_type: str, resource_id: str, meta: dict[str, Any]) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            meta_json=_json_dumps(meta),
        )
    )


def _emit(event: str, payload: dict[str, Any], *, rooms: Iterable[str]) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        for room in rooms:
            try:
                from_thread.run(sio.emit, event, {"data": payload}, room=room, namespace=settings.DASHBOARD_NAMESPACE)
            except Exception:
                return
        return

    for room in rooms:
        sio.start_background_task(
            sio.emit,
            event,
            {"data": payload},
            room=room,
            namespace=settings.DASHBOARD_NAMESPACE,
        )


def _resolve_context(db: Session, user: User) -> dict[str, Any]:
    if user.role == UserRole.homeowner:
        home = (
            db.query(Home)
            .filter(Home.homeowner_id == user.id)
            .order_by(Home.created_at.desc())
            .first()
        )
        if not home or not home.estate_id:
            raise AppException("Your home is not linked to an estate yet.", status_code=400)
        estate = db.query(Estate).filter(Estate.id == home.estate_id).first()
        if not estate:
            raise AppException("Estate not found.", status_code=404)
        return {"estate": estate, "home": home, "unitLabel": home.name}

    if not user.estate_id:
        raise AppException("This account is not linked to an estate.", status_code=400)
    estate = db.query(Estate).filter(Estate.id == user.estate_id).first()
    if not estate:
        raise AppException("Estate not found.", status_code=404)
    return {"estate": estate, "home": None, "unitLabel": None}


def _list_security_users(db: Session, estate_id: str) -> list[User]:
    return (
        db.query(User)
        .filter(
            User.estate_id == estate_id,
            User.role.in_([UserRole.security, UserRole.estate, UserRole.admin]),
            User.is_active.is_(True),
        )
        .order_by(User.full_name.asc())
        .all()
    )


def _alert_rooms(alert: EmergencyAlert, recipient_ids: list[str]) -> list[str]:
    rooms = [
        f"estate:{alert.estate_id}:alerts",
        f"estate:{alert.estate_id}:safety",
        f"user:{alert.user_id}",
    ]
    for recipient_id in recipient_ids:
        rooms.append(f"user:{recipient_id}")
    return rooms


def serialize_alert_event(event: EmergencyAlertEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "eventType": event.event_type,
        "channel": event.channel,
        "deliveryStatus": event.delivery_status.value if hasattr(event.delivery_status, "value") else str(event.delivery_status),
        "targetType": event.target_type,
        "targetUserId": event.target_user_id,
        "targetLabel": event.target_label,
        "metadata": _json_loads(event.metadata_json),
        "createdAt": event.created_at.isoformat() if event.created_at else None,
    }


def serialize_alert(db: Session, alert: EmergencyAlert) -> dict[str, Any]:
    events = (
        db.query(EmergencyAlertEvent)
        .filter(EmergencyAlertEvent.alert_id == alert.id)
        .order_by(EmergencyAlertEvent.created_at.asc())
        .all()
    )
    return {
        "id": alert.id,
        "estateId": alert.estate_id,
        "homeId": alert.home_id,
        "userId": alert.user_id,
        "alertType": alert.alert_type.value if hasattr(alert.alert_type, "value") else str(alert.alert_type),
        "priority": alert.priority.value if hasattr(alert.priority, "value") else str(alert.priority),
        "status": alert.status.value if hasattr(alert.status, "value") else str(alert.status),
        "unitLabel": alert.unit_label,
        "triggerMode": alert.trigger_mode,
        "silentTrigger": bool(alert.silent_trigger),
        "offlineQueued": bool(alert.offline_queued),
        "cancelWindowSeconds": int(alert.cancel_window_seconds or 0),
        "cancelExpiresAt": alert.cancel_expires_at.isoformat() if alert.cancel_expires_at else None,
        "acknowledgedAt": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        "escalatedAt": alert.escalated_at.isoformat() if alert.escalated_at else None,
        "resolvedAt": alert.resolved_at.isoformat() if alert.resolved_at else None,
        "location": {
            "lat": _to_float(alert.last_known_lat),
            "lng": _to_float(alert.last_known_lng),
            "address": alert.last_known_address,
            "source": alert.last_known_source,
        },
        "notes": alert.notes or "",
        "triggeredAt": alert.triggered_at.isoformat() if alert.triggered_at else None,
        "updatedAt": alert.updated_at.isoformat() if alert.updated_at else None,
        "events": [serialize_alert_event(event) for event in events],
    }


def serialize_watchlist_entry(entry: WatchlistEntry, *, recent_reports: list[VisitorReport] | None = None) -> dict[str, Any]:
    return {
        "id": entry.id,
        "estateId": entry.estate_id,
        "displayName": entry.display_name,
        "displayPhone": entry.display_phone,
        "riskLevel": entry.risk_level.value if hasattr(entry.risk_level, "value") else str(entry.risk_level),
        "reportCount": int(entry.report_count or 0),
        "active": bool(entry.active),
        "blocked": bool(entry.blocked),
        "autoFlagged": bool(entry.auto_flagged),
        "lastReportedAt": entry.last_reported_at.isoformat() if entry.last_reported_at else None,
        "history": [
            {
                "id": report.id,
                "reason": report.reason,
                "severity": report.severity.value if hasattr(report.severity, "value") else str(report.severity),
                "status": report.status.value if hasattr(report.status, "value") else str(report.status),
                "createdAt": report.created_at.isoformat() if report.created_at else None,
            }
            for report in (recent_reports or [])
        ],
    }


def serialize_visitor_report(report: VisitorReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "estateId": report.estate_id,
        "visitorSessionId": report.visitor_session_id,
        "reporterUserId": report.reporter_user_id,
        "hostUserId": report.host_user_id,
        "reportedName": report.reported_name,
        "reportedPhone": report.reported_phone,
        "reason": report.reason,
        "notes": report.notes or "",
        "severity": report.severity.value if hasattr(report.severity, "value") else str(report.severity),
        "status": report.status.value if hasattr(report.status, "value") else str(report.status),
        "occurrenceCount": int(report.occurrence_count or 1),
        "createdAt": report.created_at.isoformat() if report.created_at else None,
        "updatedAt": report.updated_at.isoformat() if report.updated_at else None,
    }


def trigger_emergency_alert(
    db: Session,
    *,
    user: User,
    alert_type: str,
    trigger_mode: str = "hold",
    silent_trigger: bool = False,
    cancel_window_seconds: int = 8,
    location: dict[str, Any] | None = None,
    offline_queued: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    context = _resolve_context(db, user)
    estate: Estate = context["estate"]
    home: Home | None = context["home"]
    settings_row = db.query(HomeownerSetting).filter(HomeownerSetting.user_id == user.id).first() if user.role == UserRole.homeowner else None

    try:
        normalized_type = EmergencyAlertType(str(alert_type).strip().lower())
    except Exception as exc:
        raise AppException("Unsupported alert type.", status_code=400) from exc

    now = utc_now()
    cancel_window_seconds = max(5, min(int(cancel_window_seconds or 8), 10))
    alert = EmergencyAlert(
        estate_id=estate.id,
        home_id=home.id if home else None,
        user_id=user.id,
        alert_type=normalized_type,
        priority=PRIORITY_BY_TYPE[normalized_type],
        status=EmergencyAlertStatus.dispatched,
        unit_label=context["unitLabel"],
        trigger_mode=(trigger_mode or "hold").strip().lower(),
        silent_trigger=bool(silent_trigger),
        offline_queued=bool(offline_queued),
        cancel_window_seconds=cancel_window_seconds,
        cancel_expires_at=now + timedelta(seconds=cancel_window_seconds),
        last_known_lat=location.get("lat") if isinstance(location, dict) else None,
        last_known_lng=location.get("lng") if isinstance(location, dict) else None,
        last_known_address=location.get("address") if isinstance(location, dict) else None,
        last_known_source=location.get("source") if isinstance(location, dict) else None,
        notes=(notes or "").strip() or None,
        triggered_at=now,
    )
    db.add(alert)
    db.flush()

    recipients = _list_security_users(db, estate.id)
    event_rows = [
        EmergencyAlertEvent(
            alert_id=alert.id,
            actor_user_id=user.id,
            event_type="triggered",
            channel="internet",
            delivery_status=AlertDeliveryStatus.sent,
            target_type="system",
            target_label="QRing realtime gateway",
            metadata_json=_json_dumps({"offlineQueued": bool(offline_queued), "silentTrigger": bool(silent_trigger)}),
        )
    ]
    for recipient in recipients:
        event_rows.append(
            EmergencyAlertEvent(
                alert_id=alert.id,
                actor_user_id=user.id,
                event_type="security_notified",
                channel="internet",
                delivery_status=AlertDeliveryStatus.received,
                target_type="user",
                target_user_id=recipient.id,
                target_label=recipient.full_name,
                metadata_json=_json_dumps({"role": recipient.role.value}),
            )
        )
    if settings_row and settings_row.sms_fallback_enabled:
        event_rows.append(
            EmergencyAlertEvent(
                alert_id=alert.id,
                actor_user_id=user.id,
                event_type="sms_fallback_queued",
                channel="sms",
                delivery_status=AlertDeliveryStatus.queued,
                target_type="fallback",
                target_label="Configured fallback contacts",
                metadata_json=_json_dumps({"source": "homeowner_settings"}),
            )
        )
    db.add_all(event_rows)
    _log_audit(
        db,
        actor_user_id=user.id,
        action="emergency_alert.triggered",
        resource_type="emergency_alert",
        resource_id=alert.id,
        meta={
            "estateId": estate.id,
            "homeId": home.id if home else None,
            "alertType": normalized_type.value,
            "silentTrigger": bool(silent_trigger),
            "offlineQueued": bool(offline_queued),
        },
    )
    db.commit()
    db.refresh(alert)

    message = f"{SECURITY_MESSAGE_BY_TYPE[normalized_type]} Unit: {context['unitLabel'] or 'Unknown'}."
    for recipient in recipients:
        create_notification(
            db=db,
            user_id=recipient.id,
            kind="safety.emergency",
            payload={
                "alertId": alert.id,
                "estateId": estate.id,
                "alertType": normalized_type.value,
                "priority": alert.priority.value,
                "unitLabel": context["unitLabel"],
                "silentTrigger": bool(silent_trigger),
                "message": message,
            },
        )

    serialized = serialize_alert(db, alert)
    _emit("safety.alert.created", serialized, rooms=_alert_rooms(alert, [recipient.id for recipient in recipients]))
    return serialized


def _load_alert_for_actor(db: Session, *, alert_id: str, actor: User) -> EmergencyAlert:
    alert = db.query(EmergencyAlert).filter(EmergencyAlert.id == alert_id).first()
    if not alert:
        raise AppException("Emergency alert not found.", status_code=404)

    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    if alert.estate_id != estate.id and actor.role != UserRole.admin:
        raise AppException("You do not have access to this emergency alert.", status_code=403)
    return alert


def _append_alert_event(
    db: Session,
    *,
    alert: EmergencyAlert,
    actor_user_id: str | None,
    event_type: str,
    channel: str,
    delivery_status: AlertDeliveryStatus,
    target_type: str | None = None,
    target_user_id: str | None = None,
    target_label: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        EmergencyAlertEvent(
            alert_id=alert.id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            channel=channel,
            delivery_status=delivery_status,
            target_type=target_type,
            target_user_id=target_user_id,
            target_label=target_label,
            metadata_json=_json_dumps(metadata),
        )
    )


def cancel_emergency_alert(db: Session, *, alert_id: str, user: User, reason: str | None = None) -> dict[str, Any]:
    alert = _load_alert_for_actor(db, alert_id=alert_id, actor=user)
    now = utc_now()
    if alert.user_id != user.id:
        raise AppException("Only the resident who triggered this alert can cancel it.", status_code=403)
    if alert.status in {EmergencyAlertStatus.acknowledged, EmergencyAlertStatus.escalated, EmergencyAlertStatus.resolved}:
        raise AppException("This alert is already being handled and can no longer be cancelled.", status_code=400)
    if alert.cancel_expires_at and now > alert.cancel_expires_at:
        raise AppException("The cancel window has expired.", status_code=400)

    alert.status = EmergencyAlertStatus.cancelled
    alert.notes = reason or alert.notes
    _append_alert_event(
        db,
        alert=alert,
        actor_user_id=user.id,
        event_type="cancelled",
        channel="internet",
        delivery_status=AlertDeliveryStatus.acknowledged,
        target_type="resident",
        target_user_id=user.id,
        target_label=user.full_name,
        metadata={"reason": reason or ""},
    )
    _log_audit(
        db,
        actor_user_id=user.id,
        action="emergency_alert.cancelled",
        resource_type="emergency_alert",
        resource_id=alert.id,
        meta={"reason": reason or ""},
    )
    db.commit()
    db.refresh(alert)
    serialized = serialize_alert(db, alert)
    _emit("safety.alert.updated", serialized, rooms=_alert_rooms(alert, []))
    return serialized


def update_emergency_alert_status(
    db: Session,
    *,
    alert_id: str,
    actor: User,
    action: str,
    notes: str | None = None,
) -> dict[str, Any]:
    alert = _load_alert_for_actor(db, alert_id=alert_id, actor=actor)
    normalized_action = str(action or "").strip().lower()
    now = utc_now()

    if normalized_action == "acknowledge":
        if alert.status == EmergencyAlertStatus.cancelled:
            raise AppException("Cancelled alerts cannot be acknowledged.", status_code=400)
        alert.status = EmergencyAlertStatus.acknowledged
        alert.acknowledged_by_user_id = actor.id
        alert.acknowledged_at = now
        event_type = "acknowledged"
    elif normalized_action == "escalate":
        if alert.status == EmergencyAlertStatus.cancelled:
            raise AppException("Cancelled alerts cannot be escalated.", status_code=400)
        alert.status = EmergencyAlertStatus.escalated
        alert.escalated_at = now
        event_type = "escalated"
    elif normalized_action == "resolve":
        alert.status = EmergencyAlertStatus.resolved
        alert.resolved_by_user_id = actor.id
        alert.resolved_at = now
        event_type = "resolved"
    else:
        raise AppException("Unsupported alert action.", status_code=400)

    if notes:
        alert.notes = notes.strip()
    _append_alert_event(
        db,
        alert=alert,
        actor_user_id=actor.id,
        event_type=event_type,
        channel="internet",
        delivery_status=AlertDeliveryStatus.acknowledged,
        target_type="operator",
        target_user_id=actor.id,
        target_label=actor.full_name,
        metadata={"notes": notes or ""},
    )
    _log_audit(
        db,
        actor_user_id=actor.id,
        action=f"emergency_alert.{event_type}",
        resource_type="emergency_alert",
        resource_id=alert.id,
        meta={"notes": notes or ""},
    )
    db.commit()
    db.refresh(alert)
    serialized = serialize_alert(db, alert)
    _emit("safety.alert.updated", serialized, rooms=_alert_rooms(alert, []))
    return serialized


def list_emergency_alerts(db: Session, *, actor: User, limit: int = 40) -> list[dict[str, Any]]:
    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    query = db.query(EmergencyAlert).filter(EmergencyAlert.estate_id == estate.id)
    if actor.role == UserRole.homeowner:
        query = query.filter(EmergencyAlert.user_id == actor.id)
    rows = query.order_by(EmergencyAlert.triggered_at.desc()).limit(max(1, min(limit, 100))).all()
    return [serialize_alert(db, row) for row in rows]


def _severity_rank(severity: VisitorReportSeverity | str) -> int:
    value = severity.value if hasattr(severity, "value") else str(severity)
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(value, 2)


def _risk_for_report_count(count: int, severity: VisitorReportSeverity | str) -> WatchlistRiskLevel:
    score = max(count, _severity_rank(severity))
    if score >= 4:
        return WatchlistRiskLevel.critical
    if score >= 3:
        return WatchlistRiskLevel.high
    if score >= 2:
        return WatchlistRiskLevel.medium
    return WatchlistRiskLevel.low


def report_visitor(
    db: Session,
    *,
    actor: User,
    visitor_session_id: str | None,
    reported_name: str | None,
    reported_phone: str | None,
    reason: str,
    notes: str | None,
    severity: str,
) -> dict[str, Any]:
    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    window_start = utc_now() - timedelta(days=1)
    reports_today = (
        db.query(func.count(VisitorReport.id))
        .filter(VisitorReport.reporter_user_id == actor.id, VisitorReport.created_at >= window_start)
        .scalar()
        or 0
    )
    if reports_today >= MAX_VISITOR_REPORTS_PER_DAY:
        raise AppException("Daily visitor report limit reached. Please contact estate admin for urgent cases.", status_code=429)

    session = None
    if visitor_session_id:
        session = db.query(VisitorSession).filter(VisitorSession.id == visitor_session_id).first()
        if not session:
            raise AppException("Visitor record not found.", status_code=404)
        if session.estate_id != estate.id:
            raise AppException("Visitor record does not belong to your estate.", status_code=403)

    try:
        normalized_severity = VisitorReportSeverity(str(severity or "medium").strip().lower())
    except Exception as exc:
        raise AppException("Unsupported report severity.", status_code=400) from exc

    final_name = (reported_name or (session.visitor_label if session else "")).strip()
    if not final_name:
        raise AppException("Visitor name is required.", status_code=400)
    final_phone = (reported_phone or (session.visitor_phone if session else "")).strip() or None

    normalized_name = _normalize_name(final_name)
    normalized_phone = _normalize_phone(final_phone)
    watchlist = (
        db.query(WatchlistEntry)
        .filter(
            WatchlistEntry.estate_id == estate.id,
            WatchlistEntry.normalized_name == normalized_name,
            WatchlistEntry.normalized_phone == (normalized_phone or None),
        )
        .first()
    )

    report = VisitorReport(
        estate_id=estate.id,
        visitor_session_id=session.id if session else None,
        reporter_user_id=actor.id,
        host_user_id=session.homeowner_id if session else None,
        reported_name=final_name,
        reported_phone=final_phone,
        reason=(reason or "").strip(),
        notes=(notes or "").strip() or None,
        severity=normalized_severity,
        status=VisitorReportStatus.pending_review,
    )
    if not report.reason:
        raise AppException("Reason for report is required.", status_code=400)
    db.add(report)
    db.flush()

    if watchlist:
        watchlist.display_name = final_name
        watchlist.display_phone = final_phone
        watchlist.report_count = int(watchlist.report_count or 0) + 1
        watchlist.latest_report_id = report.id
        watchlist.last_reported_at = utc_now()
        watchlist.auto_flagged = bool(watchlist.report_count >= 2 or normalized_severity == VisitorReportSeverity.critical)
        watchlist.risk_level = _risk_for_report_count(watchlist.report_count, normalized_severity)
        report.occurrence_count = int(watchlist.report_count or 1)
    else:
        watchlist = WatchlistEntry(
            estate_id=estate.id,
            latest_report_id=report.id,
            display_name=final_name,
            display_phone=final_phone,
            normalized_name=normalized_name,
            normalized_phone=normalized_phone or None,
            report_count=1,
            auto_flagged=normalized_severity in {VisitorReportSeverity.high, VisitorReportSeverity.critical},
            risk_level=_risk_for_report_count(1, normalized_severity),
            last_reported_at=utc_now(),
        )
        db.add(watchlist)

    _log_audit(
        db,
        actor_user_id=actor.id,
        action="visitor_report.created",
        resource_type="visitor_report",
        resource_id=report.id,
        meta={
            "estateId": estate.id,
            "visitorSessionId": session.id if session else None,
            "watchlistEntryId": watchlist.id,
            "severity": normalized_severity.value,
        },
    )
    db.commit()
    db.refresh(report)
    db.refresh(watchlist)

    recipients = _list_security_users(db, estate.id)
    for recipient in recipients:
        create_notification(
            db=db,
            user_id=recipient.id,
            kind="safety.visitor_report",
            payload={
                "reportId": report.id,
                "estateId": estate.id,
                "reportedName": final_name,
                "severity": normalized_severity.value,
                "watchlistEntryId": watchlist.id,
                "message": f"Visitor report submitted for {final_name}. Review watchlist activity.",
            },
        )

    report_payload = serialize_visitor_report(report)
    watchlist_payload = get_watchlist(db, actor=actor, limit=20)
    _emit("safety.report.created", report_payload, rooms=[f"estate:{estate.id}:safety"])
    _emit("safety.watchlist.updated", {"items": watchlist_payload}, rooms=[f"estate:{estate.id}:safety"])
    return {
        "report": report_payload,
        "watchlistEntry": serialize_watchlist_entry(
            watchlist,
            recent_reports=[
                row
                for row in db.query(VisitorReport)
                .filter(
                    VisitorReport.estate_id == estate.id,
                    VisitorReport.reported_name == watchlist.display_name,
                )
                .order_by(VisitorReport.created_at.desc())
                .limit(5)
                .all()
            ],
        ),
    }


def get_watchlist(db: Session, *, actor: User, limit: int = 30) -> list[dict[str, Any]]:
    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    entries = (
        db.query(WatchlistEntry)
        .filter(WatchlistEntry.estate_id == estate.id)
        .order_by(WatchlistEntry.last_reported_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    items: list[dict[str, Any]] = []
    for entry in entries:
        reports = (
            db.query(VisitorReport)
            .filter(VisitorReport.estate_id == estate.id, VisitorReport.reported_name == entry.display_name)
            .order_by(VisitorReport.created_at.desc())
            .limit(5)
            .all()
        )
        items.append(serialize_watchlist_entry(entry, recent_reports=reports))
    return items


def get_safety_dashboard(db: Session, *, actor: User) -> dict[str, Any]:
    context = _resolve_context(db, actor)
    estate: Estate = context["estate"]
    alerts = list_emergency_alerts(db, actor=actor, limit=20)
    reports_query = db.query(VisitorReport).filter(VisitorReport.estate_id == estate.id)
    if actor.role == UserRole.homeowner:
        reports_query = reports_query.filter(VisitorReport.host_user_id == actor.id)
    reports = reports_query.order_by(VisitorReport.created_at.desc()).limit(20).all()
    watchlist = get_watchlist(db, actor=actor, limit=12)
    active_alerts = [row for row in alerts if row["status"] not in {"resolved", "cancelled"}]
    return {
        "context": {
            "estateId": estate.id,
            "estateName": estate.name,
            "unitLabel": context["unitLabel"],
            "role": actor.role.value,
        },
        "metrics": {
            "activeAlerts": len(active_alerts),
            "criticalAlerts": len([row for row in active_alerts if row["priority"] == "critical"]),
            "watchlistCount": len(watchlist),
            "pendingReports": len([row for row in reports if row.status == VisitorReportStatus.pending_review]),
        },
        "alerts": alerts,
        "reports": [serialize_visitor_report(row) for row in reports],
        "watchlist": watchlist,
        "architecture": {
            "delivery": [
                "Primary: realtime dashboard/socket delivery over internet",
                "Secondary: queued SMS fallback events when SMS fallback is enabled",
                "Offline: trigger stored with offlineQueued flag and replay-safe request flow",
            ],
            "liability": "QRing facilitates emergency alerting and coordination. It does not guarantee police, fire, or medical response.",
        },
    }
