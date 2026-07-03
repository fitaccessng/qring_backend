from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from functools import partial
from datetime import datetime
from typing import Any

from anyio import from_thread
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.db.models import CallSession, Home, Message, Notification, Office, OfficeMember, User, VisitorSession
from app.services.notification_service import create_notification
from app.services.realtime_notification_service import (
    build_notification_envelope,
    build_notification_idempotency_key,
    emit_dashboard_notification,
    emit_signaling_notification,
)
from app.services.call_service import end_call_session, mark_call_session_answered, mark_call_session_rejected, start_call_session
from app.services.realtime_config_service import build_webrtc_rtc_config

OFFICE_QUEUE_STATUSES = {"pending", "submitted", "received_by_security", "forwarded_to_homeowner"}
OFFICE_ACTIVE_STATUSES = {"approved", "active", "checked_in"}
OFFICE_FINAL_STATUSES = {"rejected", "completed", "checked_out", "cancelled", "expired"}


def _status_label(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"pending", "forwarded", "submitted", "received_by_security", "forwarded_to_homeowner"}:
        return "Awaiting approval"
    if normalized in {"approved", "active", "checked_in"}:
        return "Inside office"
    if normalized in {"rejected"}:
        return "Rejected"
    if normalized in {"completed", "checked_out", "cancelled", "expired"}:
        return normalized.replace("_", " ").title()
    return normalized.title() or "Update"


def _find_office_for_user(db: Session, user_id: str) -> Office | None:
    member = (
        db.query(OfficeMember)
        .join(Office, Office.id == OfficeMember.office_id)
        .filter(OfficeMember.user_id == user_id)
        .order_by(OfficeMember.created_at.desc())
        .first()
    )
    if member:
        return member.office
    return db.query(Office).filter(Office.administrator_user_id == user_id).first()


def _office_homes_query(db: Session, office_id: str):
    return db.query(Home).filter(Home.office_id == office_id)


def _office_sessions_query(db: Session, office_id: str):
    return (
        db.query(VisitorSession)
        .join(Home, Home.id == VisitorSession.home_id)
        .filter(Home.office_id == office_id)
    )


def _office_messages_query(db: Session, office_id: str):
    return (
        db.query(Message)
        .join(VisitorSession, Message.session_id == VisitorSession.id)
        .join(Home, Home.id == VisitorSession.home_id)
        .filter(Home.office_id == office_id)
    )


def _office_room_ids(office: Office) -> list[str]:
    return [f"office:{office.id}", f"office:{office.id}:reception", f"office:{office.id}:security"]


def _office_employee_room(office_id: str, user_id: str) -> str:
    return f"office:{office_id}:employee:{user_id}"


def _office_call_room_ids(office: Office, session_id: str | None = None, call_session_id: str | None = None) -> list[str]:
    rooms = set(_office_room_ids(office))
    if session_id:
        rooms.add(f"session:{session_id}")
    if call_session_id:
        rooms.add(f"call:{call_session_id}")
    return list(rooms)


def _office_direct_call_room_ids(
    office: Office,
    *,
    call_session_id: str | None = None,
    caller_user_id: str | None = None,
    receiver_user_id: str | None = None,
) -> list[str]:
    rooms = set(_office_room_ids(office))
    if call_session_id:
        rooms.add(f"call:{call_session_id}")
    if caller_user_id:
        rooms.add(_office_employee_room(office.id, caller_user_id))
    if receiver_user_id:
        rooms.add(_office_employee_room(office.id, receiver_user_id))
    return list(rooms)


def _office_member_or_admin_exists(db: Session, *, office_id: str, user_id: str) -> bool:
    member = (
        db.query(OfficeMember.id)
        .filter(OfficeMember.office_id == office_id, OfficeMember.user_id == user_id)
        .first()
    )
    if member:
        return True
    return bool(db.query(Office.id).filter(Office.id == office_id, Office.administrator_user_id == user_id).first())


def _office_call_target_user(
    db: Session,
    *,
    office: Office,
    target_role: str | None,
    employee_id: str | None = None,
    reception_id: str | None = None,
    security_id: str | None = None,
) -> str | None:
    normalized_role = str(target_role or "visitor").strip().lower() or "visitor"
    if normalized_role == "visitor":
        return None

    target_user_id = {
        "employee": str(employee_id or "").strip() or None,
        "reception": str(reception_id or "").strip() or None,
        "security": str(security_id or "").strip() or None,
    }.get(normalized_role)
    if normalized_role not in {"employee", "reception", "security"}:
        raise ValueError("Invalid target role")
    if not target_user_id:
        raise ValueError(f"{normalized_role}Id is required")

    if normalized_role == "employee":
        if not _office_member_or_admin_exists(db, office_id=office.id, user_id=target_user_id):
            raise ValueError("Target employee is not part of this office")
        return target_user_id

    if normalized_role in {"reception", "security"}:
        if not _office_member_or_admin_exists(db, office_id=office.id, user_id=target_user_id):
            raise ValueError(f"Target {normalized_role} is not part of this office")
        return target_user_id

    return None


def _office_call_lookup(db: Session, *, office_id: str, call_session_id: str) -> tuple[Office | None, CallSession | None, VisitorSession | None]:
    office = db.query(Office).filter(Office.id == office_id).first()
    if not office:
        return None, None, None
    call_session = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not call_session:
        return office, None, None
    session = None
    if call_session.visitor_session_id:
        session = _office_sessions_query(db, office.id).filter(VisitorSession.id == call_session.visitor_session_id).first()
    if not session and call_session.appointment_id:
        session = (
            db.query(VisitorSession)
            .filter(VisitorSession.appointment_id == call_session.appointment_id)
            .join(Home, Home.id == VisitorSession.home_id)
            .filter(Home.office_id == office.id)
            .first()
        )
    return office, call_session, session


def _find_active_office_direct_call(
    db: Session,
    *,
    office: Office,
    caller_id: str,
    receiver_id: str,
) -> CallSession | None:
    return (
        db.query(CallSession)
        .filter(
            CallSession.homeowner_id == office.administrator_user_id,
            CallSession.caller_id == caller_id,
            CallSession.receiver_id == receiver_id,
            CallSession.visitor_session_id.is_(None),
            CallSession.appointment_id.is_(None),
            CallSession.status.in_(OFFICE_QUEUE_STATUSES | OFFICE_ACTIVE_STATUSES),
        )
        .order_by(CallSession.created_at.desc())
        .first()
    )


def _create_office_direct_call_session(
    db: Session,
    *,
    office: Office,
    caller_id: str,
    receiver_id: str,
    call_type: str,
) -> CallSession:
    active = _find_active_office_direct_call(db, office=office, caller_id=caller_id, receiver_id=receiver_id)
    if active:
        return active
    row = CallSession(
        id=str(uuid.uuid4()),
        appointment_id=None,
        visitor_session_id=None,
        security_user_id=caller_id,
        caller_id=caller_id,
        receiver_id=receiver_id,
        call_type=call_type,
        room_name=f"qring-call-{uuid.uuid4()}",
        visitor_id=f"office:{office.id}:employee:{receiver_id}",
        homeowner_id=office.administrator_user_id,
        visitor_request_id=f"office:{office.id}",
        initiated_by_role="office",
        status="ringing",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def request_office_call(
    db: Session,
    *,
    user_id: str,
    visitor_session_id: str | None = None,
    call_type: str = "audio",
    has_video: bool | None = None,
    target_role: str | None = None,
    employee_id: str | None = None,
    reception_id: str | None = None,
    security_id: str | None = None,
    visitor_name: str | None = None,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")

    receiver_id = _office_call_target_user(
        db,
        office=office,
        target_role=target_role,
        employee_id=employee_id,
        reception_id=reception_id,
        security_id=security_id,
    )
    call_kind = (call_type or "").strip().lower() or ("video" if has_video else "audio")
    session = None
    if visitor_session_id:
        session = _office_sessions_query(db, office.id).filter(VisitorSession.id == visitor_session_id).first()
        if not session:
            raise ValueError("Visitor session not found")
        row = from_thread.run(
            partial(
                start_call_session,
                db,
                visitor_session_id=session.id,
                visitor_id=session.id,
                homeowner_id=session.homeowner_id,
                security_user_id=user_id,
                caller_id=user_id,
                receiver_id=receiver_id or session.homeowner_id,
                call_type=call_kind,
                visitor_name=visitor_name or session.visitor_label,
            )
        )
    elif str(target_role or "").strip().lower() == "employee":
        if not receiver_id:
            raise ValueError("employeeId is required")
        row = _create_office_direct_call_session(
            db,
            office=office,
            caller_id=user_id,
            receiver_id=receiver_id,
            call_type=call_kind,
        )
    else:
        raise ValueError("visitorSessionId is required for this call target")
    caller = db.query(User).filter(User.id == user_id).first()
    caller_name = (caller.full_name if caller else "") or "Office"
    target_user_id = receiver_id or (session.homeowner_id if session else None)
    payload_session_id = session.id if session else row.id
    room_ids = (
        _office_call_room_ids(office, session_id=session.id, call_session_id=row.id)
        if session
        else _office_direct_call_room_ids(
            office,
            call_session_id=row.id,
            caller_user_id=user_id,
            receiver_user_id=target_user_id,
        )
    )
    payload = {
        "eventId": row.id,
        "sessionId": payload_session_id,
        "callSessionId": row.id,
        "appointmentId": row.appointment_id,
        "roomName": row.room_name,
        "deliveryRoom": f"call:{row.id}",
        "status": row.status,
        "visitorId": row.visitor_id,
        "hasVideo": bool(has_video if has_video is not None else (call_type == "video")),
        "type": row.call_type,
        "role": caller.role.value if caller and caller.role else "office",
        "callerName": caller_name,
        "callerRole": "office",
        "callerOrigin": "office dashboard",
        "homeownerName": session.visitor_label if session else (visitor_name or caller_name or "Office"),
        "receiverId": target_user_id,
        "receiverRole": str(target_role or ("employee" if not session else "visitor")),
        "officeId": office.id,
        "callScope": "employee" if not session else "visitor",
        "message": f"{caller_name} is calling from the office dashboard.",
    }
    _emit_office_event(
        event_name="office.call.requested",
        office=office,
        payload=payload,
        source="office.call.request",
        rooms=room_ids,
    )
    from_thread.run(
        partial(
            emit_signaling_notification,
            event_name="call.requested",
            rooms=set(room_ids),
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="call.requested",
                idempotency_key=build_notification_idempotency_key(
                    event_type="call.requested",
                    user_id=office.administrator_user_id,
                    session_id=payload_session_id,
                    entity_id=row.id,
                    action=row.status,
                ),
                session_id=payload_session_id,
                user_id=user_id,
                source="office.call.request",
                payload=payload,
            ),
            idempotency_key=row.id,
            source="office.call.request",
        )
    )
    return {
        "callSessionId": row.id,
        "sessionId": payload_session_id,
        "visitorSessionId": session.id if session else None,
        "visitorId": session.id if session else row.id,
        "roomName": row.room_name,
        "status": row.status,
        "callType": row.call_type,
        "rtcConfig": build_webrtc_rtc_config(),
        "office": _office_payload(office),
        "visitorName": session.visitor_label if session else (visitor_name or caller_name or "Office"),
    }


def accept_office_call(db: Session, *, user_id: str, call_session_id: str) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise ValueError("Call session not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == row.visitor_session_id).first() if row.visitor_session_id else None
    is_direct_office_call = not row.visitor_session_id and not row.appointment_id
    if is_direct_office_call and user_id not in {row.caller_id, row.receiver_id}:
        raise ValueError("Call session does not belong to this office")
    if not session and not is_direct_office_call and row.visitor_request_id != f"office:{office.id}":
        raise ValueError("Call session does not belong to this office")
    mark_call_session_answered(db, call_session_id=call_session_id)
    payload = {
        "eventId": row.id,
        "sessionId": session.id if session else row.id,
        "callSessionId": row.id,
        "status": "accepted",
        "hasVideo": row.call_type == "video",
    }
    room_ids = (
        _office_call_room_ids(office, session_id=session.id, call_session_id=row.id)
        if session
        else _office_direct_call_room_ids(
            office,
            call_session_id=row.id,
            caller_user_id=row.caller_id,
            receiver_user_id=row.receiver_id,
        )
    )
    _emit_office_event(
        event_name="office.call.accepted",
        office=office,
        payload=payload,
        source="office.call.accept",
        rooms=room_ids,
    )
    from_thread.run(
        partial(
            emit_signaling_notification,
            event_name="call.accepted",
            rooms=set(room_ids),
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="call.accepted",
                idempotency_key=build_notification_idempotency_key(
                    event_type="call.accepted",
                    user_id=office.administrator_user_id,
                    session_id=session.id if session else row.id,
                    entity_id=row.id,
                    action="accepted",
                ),
                session_id=session.id if session else row.id,
                user_id=user_id,
                source="office.call.accept",
                payload=payload,
            ),
            idempotency_key=f"{row.id}:accepted",
            source="office.call.accept",
        )
    )
    return {"callSessionId": row.id, "status": "accepted"}


def reject_office_call(db: Session, *, user_id: str, call_session_id: str) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise ValueError("Call session not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == row.visitor_session_id).first() if row.visitor_session_id else None
    is_direct_office_call = not row.visitor_session_id and not row.appointment_id
    if is_direct_office_call and user_id not in {row.caller_id, row.receiver_id}:
        raise ValueError("Call session does not belong to this office")
    if not session and not is_direct_office_call and row.visitor_request_id != f"office:{office.id}":
        raise ValueError("Call session does not belong to this office")
    mark_call_session_rejected(db, call_session_id=call_session_id, reason="rejected")
    payload = {
        "eventId": row.id,
        "sessionId": session.id if session else row.id,
        "callSessionId": row.id,
        "status": "rejected",
        "hasVideo": row.call_type == "video",
    }
    room_ids = (
        _office_call_room_ids(office, session_id=session.id, call_session_id=row.id)
        if session
        else _office_direct_call_room_ids(
            office,
            call_session_id=row.id,
            caller_user_id=row.caller_id,
            receiver_user_id=row.receiver_id,
        )
    )
    _emit_office_event(
        event_name="office.call.rejected",
        office=office,
        payload=payload,
        source="office.call.reject",
        rooms=room_ids,
    )
    from_thread.run(
        partial(
            emit_signaling_notification,
            event_name="call.rejected",
            rooms=set(room_ids),
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="call.rejected",
                idempotency_key=build_notification_idempotency_key(
                    event_type="call.rejected",
                    user_id=office.administrator_user_id,
                    session_id=session.id if session else row.id,
                    entity_id=row.id,
                    action="rejected",
                ),
                session_id=session.id if session else row.id,
                user_id=user_id,
                source="office.call.reject",
                payload=payload,
            ),
            idempotency_key=f"{row.id}:rejected",
            source="office.call.reject",
        )
    )
    return {"callSessionId": row.id, "status": "rejected"}


def end_office_call(db: Session, *, user_id: str, call_session_id: str) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise ValueError("Call session not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == row.visitor_session_id).first() if row.visitor_session_id else None
    is_direct_office_call = not row.visitor_session_id and not row.appointment_id
    if is_direct_office_call and user_id not in {row.caller_id, row.receiver_id}:
        raise ValueError("Call session does not belong to this office")
    if not session and not is_direct_office_call and row.visitor_request_id != f"office:{office.id}":
        raise ValueError("Call session does not belong to this office")
    ended_row = from_thread.run(partial(end_call_session, db, call_session_id=call_session_id))
    payload = {
        "eventId": row.id,
        "sessionId": session.id if session else row.id,
        "callSessionId": row.id,
        "status": ended_row.status if ended_row else row.status,
        "hasVideo": row.call_type == "video",
    }
    room_ids = (
        _office_call_room_ids(office, session_id=session.id, call_session_id=row.id)
        if session
        else _office_direct_call_room_ids(
            office,
            call_session_id=row.id,
            caller_user_id=row.caller_id,
            receiver_user_id=row.receiver_id,
        )
    )
    _emit_office_event(
        event_name="office.call.ended",
        office=office,
        payload=payload,
        source="office.call.end",
        rooms=room_ids,
    )
    from_thread.run(
        partial(
            emit_signaling_notification,
            event_name="call.ended",
            rooms=set(room_ids),
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="call.ended",
                idempotency_key=build_notification_idempotency_key(
                    event_type="call.ended",
                    user_id=office.administrator_user_id,
                    session_id=session.id if session else row.id,
                    entity_id=row.id,
                    action=row.status,
                ),
                session_id=session.id if session else row.id,
                user_id=user_id,
                source="office.call.end",
                payload=payload,
            ),
            idempotency_key=f"{row.id}:ended",
            source="office.call.end",
        )
    )
    return {"callSessionId": row.id, "status": row.status}


def _session_queue_item(session: VisitorSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "visitorName": session.visitor_label or "Visitor",
        "visitorPhone": session.visitor_phone or "",
        "company": "",
        "purpose": session.purpose or "",
        "employeeToVisit": "",
        "department": "",
        "status": _status_label(session.status),
        "rawStatus": session.status,
        "time": session.started_at.isoformat() if session.started_at else utc_now().isoformat(),
        "snapshotUrl": session.snapshot_url or session.photo_url or "",
        "homeId": session.home_id,
        "doorId": session.door_id,
        "hostName": "",
        "vehicleInfo": "",
    }


def _session_visitors_item(session: VisitorSession) -> dict[str, Any]:
    ended = session.ended_at or session.state_updated_at
    duration_seconds = None
    if ended and session.started_at:
        duration_seconds = max(0, int((ended - session.started_at).total_seconds()))
    return {
        "id": session.id,
        "visitorName": session.visitor_label or "Visitor",
        "visitorPhone": session.visitor_phone or "",
        "company": "",
        "purpose": session.purpose or "",
        "employeeToVisit": "",
        "department": "",
        "status": session.status,
        "statusLabel": _status_label(session.status),
        "checkedInAt": session.homeowner_decision_at.isoformat() if session.homeowner_decision_at else None,
        "checkedOutAt": session.ended_at.isoformat() if session.ended_at else None,
        "durationSeconds": duration_seconds,
        "snapshotUrl": session.snapshot_url or session.photo_url or "",
        "homeId": session.home_id,
        "doorId": session.door_id,
        "hostName": "",
        "vehicleInfo": "",
        "time": session.started_at.isoformat() if session.started_at else utc_now().isoformat(),
    }


def _emit_office_event(
    *,
    event_name: str,
    office: Office,
    payload: dict[str, Any],
    source: str,
    rooms: list[str] | None = None,
) -> None:
    envelope = build_notification_envelope(
        event_type=event_name,
        payload=payload,
        session_id=payload.get("sessionId"),
        user_id=office.administrator_user_id,
        source=source,
    )
    idempotency_key = build_notification_idempotency_key(
        event_type=event_name,
        user_id=office.administrator_user_id,
        session_id=payload.get("sessionId"),
        entity_id=payload.get("sessionId"),
        action=source,
    )
    room_list = rooms or _office_room_ids(office)
    try:
        from_thread.run(
            partial(
                emit_dashboard_notification,
                event_name=event_name,
                payload=envelope,
                idempotency_key=idempotency_key,
                source=source,
                rooms=room_list,
            ),
        )
    except Exception:
        # Best-effort only. Queue state should still persist even if realtime is down.
        pass


def _office_payload(office: Office) -> dict[str, Any]:
    return {
        "id": office.id,
        "companyName": office.company_name,
        "businessEmail": office.business_email,
        "phoneNumber": office.phone_number,
        "officeAddress": office.office_address,
        "country": office.country,
        "state": office.state,
        "city": office.city,
        "officeSize": office.office_size,
        "industry": office.industry,
        "employeeCount": office.employee_count,
        "timezone": office.timezone,
        "qrId": office.qr_id,
        "scanUrl": f"/scan/{office.qr_id}",
        "receptionHomeId": office.reception_home_id,
        "receptionDoorId": office.reception_door_id,
    }


def _base_empty_response() -> dict[str, Any]:
    return {
        "office": None,
        "metrics": {
            "liveQueue": 0,
            "pendingApprovals": 0,
            "visitorsInside": 0,
            "employeesOnline": 0,
            "todayAppointments": 0,
            "recentConversations": 0,
            "securityAlerts": 0,
            "recentDeliveries": 0,
        },
        "recentQueue": [],
        "recentActivity": [],
        "recentMessages": [],
        "securityAlerts": [],
        "quickActions": [],
        "employees": [],
    }


def _get_office_data(db: Session, user_id: str) -> tuple[Office | None, list[VisitorSession], list[OfficeMember], list[Message], list[Notification]]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return None, [], [], [], []

    sessions = _office_sessions_query(db, office.id).order_by(VisitorSession.started_at.desc()).all()
    members = (
        db.query(OfficeMember)
        .filter(OfficeMember.office_id == office.id)
        .order_by(OfficeMember.created_at.desc())
        .all()
    )
    messages = _office_messages_query(db, office.id).order_by(Message.created_at.desc()).limit(30).all()
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == office.administrator_user_id)
        .order_by(Notification.created_at.desc())
        .limit(30)
        .all()
    )
    return office, sessions, members, messages, notifications


def get_office_overview(db: Session, *, user_id: str) -> dict[str, Any]:
    office, sessions, members, messages, notifications = _get_office_data(db, user_id)
    if not office:
        return _base_empty_response()

    queue_sessions = [session for session in sessions if str(session.status or "").lower() in OFFICE_QUEUE_STATUSES]
    active_sessions = [session for session in sessions if str(session.status or "").lower() in OFFICE_ACTIVE_STATUSES]
    delivery_sessions = [session for session in sessions if str(session.visitor_type or "").lower() == "delivery"]
    alert_notifications = [item for item in notifications if "alert" in str(item.kind or "").lower()]

    recent_messages = [
        {
            "id": message.id,
            "sessionId": message.session_id,
            "from": message.sender_type,
            "text": message.body,
            "time": message.created_at.isoformat(),
            "unread": message.read_by_homeowner_at is None and message.sender_type != "homeowner",
        }
        for message in messages[:6]
    ]

    activity: list[dict[str, Any]] = []
    for session in sessions[:6]:
        activity.append(
            {
                "id": f"session-{session.id}",
                "event": session.visitor_label or "Visitor",
                "details": session.purpose or "Office visitor request",
                "time": session.started_at.isoformat() if session.started_at else utc_now().isoformat(),
                "state": session.status,
            }
        )
    for note in notifications[:6]:
        try:
            payload = json.loads(note.payload or "{}")
        except Exception:
            payload = {}
        activity.append(
            {
                "id": note.id,
                "event": str(note.kind or "notification").replace(".", " ").title(),
                "details": str(payload.get("message") or "Office activity"),
                "time": note.created_at.isoformat() if note.created_at else utc_now().isoformat(),
                "state": "notification",
            }
        )
    activity.sort(key=lambda item: item.get("time") or "", reverse=True)

    availability_counts = Counter(str(member.availability_status or "unknown").lower() for member in members)
    active_employee_count = len([member for member in members if str(member.status or "").lower() == "active"])

    return {
        "office": _office_payload(office),
        "metrics": {
            "liveQueue": len(queue_sessions),
            "pendingApprovals": len(queue_sessions),
            "visitorsInside": len(active_sessions),
            "employeesOnline": active_employee_count,
            "todayAppointments": 0,
            "recentConversations": len(messages),
            "securityAlerts": len(alert_notifications),
            "recentDeliveries": len(delivery_sessions),
        },
        "recentQueue": [_session_queue_item(session) for session in queue_sessions[:5]],
        "recentActivity": activity[:6],
        "recentMessages": recent_messages,
        "securityAlerts": [
            {
                "id": item.id,
                "event": str(item.kind or "alert").replace(".", " ").title(),
                "details": (json.loads(item.payload or "{}").get("message") if item.payload else "") or "Security alert",
                "time": item.created_at.isoformat() if item.created_at else utc_now().isoformat(),
            }
            for item in alert_notifications[:6]
        ],
        "quickActions": [
            {"label": "Live Queue", "to": "/dashboard/office/queue"},
            {"label": "Visitors", "to": "/dashboard/office/visitors"},
            {"label": "Messages", "to": "/dashboard/office/messages"},
            {"label": "Employees", "to": "/dashboard/office/employees"},
            {"label": "QR Entry", "to": "/dashboard/office/qr"},
            {"label": "Settings", "to": "/dashboard/office/settings"},
        ],
        "employees": [
            {
                "id": member.id,
                "name": member.full_name,
                "role": member.role_label,
                "department": member.department,
                "floor": member.floor,
                "extension": member.extension,
                "availability": member.availability_status,
                "status": member.status,
            }
            for member in members
        ],
        "availability": dict(availability_counts),
    }


def list_office_queue(
    db: Session,
    *,
    user_id: str,
    search: str | None = None,
    status: str | None = None,
    department: str | None = None,
    employee: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": [], "metrics": {"liveQueue": 0, "pendingApprovals": 0}}
    sessions = _office_sessions_query(db, office.id).order_by(VisitorSession.started_at.desc()).all()
    items = [
        _session_queue_item(session)
        for session in sessions
        if str(session.status or "").lower() in OFFICE_QUEUE_STATUSES
    ]
    term = str(search or "").strip().lower()
    if term:
        items = [
            item
            for item in items
            if term in str(item.get("visitorName") or "").lower()
            or term in str(item.get("visitorPhone") or "").lower()
            or term in str(item.get("purpose") or "").lower()
            or term in str(item.get("company") or "").lower()
        ]
    if status:
        normalized = str(status).strip().lower()
        items = [item for item in items if str(item.get("rawStatus") or "").lower() == normalized or str(item.get("status") or "").lower() == normalized]
    if employee:
        emp = str(employee).strip().lower()
        items = [item for item in items if emp in str(item.get("employeeToVisit") or "").lower()]
    if department:
        dept = str(department).strip().lower()
        items = [item for item in items if dept in str(item.get("department") or "").lower()]
    return {
        "office": _office_payload(office),
        "items": items[:limit],
        "metrics": {"liveQueue": len(items), "pendingApprovals": len(items)},
    }


def list_office_visitors(
    db: Session,
    *,
    user_id: str,
    search: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": []}
    sessions = _office_sessions_query(db, office.id).order_by(VisitorSession.started_at.desc()).all()
    items = [_session_visitors_item(session) for session in sessions]
    term = str(search or "").strip().lower()
    if term:
        items = [
            item
            for item in items
            if term in str(item.get("visitorName") or "").lower()
            or term in str(item.get("visitorPhone") or "").lower()
            or term in str(item.get("purpose") or "").lower()
        ]
    if status:
        normalized = str(status).strip().lower()
        items = [item for item in items if str(item.get("status") or "").lower() == normalized]
    return {"office": _office_payload(office), "items": items[:limit]}


def list_office_employees(
    db: Session,
    *,
    user_id: str,
    search: str | None = None,
    department: str | None = None,
    role: str | None = None,
    availability: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": []}
    members = (
        db.query(OfficeMember)
        .filter(OfficeMember.office_id == office.id)
        .order_by(OfficeMember.created_at.desc())
        .all()
    )
    items = [
        {
            "id": member.id,
            "name": member.full_name,
            "role": member.role_label,
            "department": member.department,
            "floor": member.floor,
            "extension": member.extension,
            "availability": member.availability_status,
            "status": member.status,
            "userId": member.user_id,
        }
        for member in members
    ]
    term = str(search or "").strip().lower()
    if term:
        items = [
            item
            for item in items
            if term in str(item.get("name") or "").lower()
            or term in str(item.get("role") or "").lower()
            or term in str(item.get("department") or "").lower()
        ]
    if department:
        dept = str(department).strip().lower()
        items = [item for item in items if dept in str(item.get("department") or "").lower()]
    if role:
        role_term = str(role).strip().lower()
        items = [item for item in items if role_term in str(item.get("role") or "").lower()]
    if availability:
        avail = str(availability).strip().lower()
        items = [item for item in items if avail in str(item.get("availability") or "").lower()]
    return {"office": _office_payload(office), "items": items[:limit]}


def list_office_conversations(
    db: Session,
    *,
    user_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": []}
    sessions = _office_sessions_query(db, office.id).order_by(VisitorSession.started_at.desc()).limit(limit).all()
    messages = _office_messages_query(db, office.id).order_by(Message.created_at.desc()).all()
    latest_by_session: dict[str, Message] = {}
    unread_by_session: dict[str, int] = defaultdict(int)
    for message in messages:
      if message.session_id not in latest_by_session:
          latest_by_session[message.session_id] = message
      if message.sender_type != "office" and message.read_by_homeowner_at is None:
          unread_by_session[message.session_id] += 1
    items = []
    for session in sessions:
        latest = latest_by_session.get(session.id)
        items.append(
            {
                "id": session.id,
                "sessionId": session.id,
                "name": session.visitor_label or "Visitor",
                "last": latest.body if latest else (session.purpose or "Office conversation"),
                "unread": unread_by_session.get(session.id, 0),
                "time": (latest.created_at.isoformat() if latest else session.started_at.isoformat()),
                "visitorName": session.visitor_label or "Visitor",
                "status": session.status,
                "snapshotUrl": session.snapshot_url or session.photo_url or "",
                "purpose": session.purpose or "",
            }
        )
    return {"office": _office_payload(office), "items": items}


def list_office_conversation_messages(
    db: Session,
    *,
    user_id: str,
    session_id: str,
    limit: int = 300,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "conversation": None, "items": []}
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == session_id).first()
    if not session:
        return {"office": _office_payload(office), "conversation": None, "items": []}
    rows = db.query(Message).filter(Message.session_id == session_id).order_by(Message.created_at.asc()).limit(limit).all()
    items = [
        {
            "id": row.id,
            "sessionId": row.session_id,
            "text": row.body,
            "senderType": row.sender_type,
            "displayName": "Office" if row.sender_type == "office" else "Visitor" if row.sender_type == "visitor" else "Homeowner",
            "time": row.created_at.isoformat(),
            "read": row.sender_type == "office" or row.read_by_homeowner_at is not None,
        }
        for row in rows
    ]
    return {
        "office": _office_payload(office),
        "conversation": {
            "id": session.id,
            "visitorName": session.visitor_label or "Visitor",
            "purpose": session.purpose or "",
            "status": session.status,
            "snapshotUrl": session.snapshot_url or session.photo_url or "",
        },
        "items": items,
    }


def create_office_message(
    db: Session,
    *,
    user_id: str,
    session_id: str,
    text: str,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == session_id).first()
    if not session:
        raise ValueError("Conversation not found")
    body = (text or "").strip()
    if not body:
        raise ValueError("Message text is required")
    message = Message(
        session_id=session_id,
        sender_type="office",
        sender_id=user_id,
        receiver_id=session.homeowner_id,
        body=body,
        created_at=utc_now(),
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    create_notification(
        db=db,
        user_id=session.homeowner_id,
        kind="office.message.created",
        payload={
            "sessionId": session.id,
            "messageId": message.id,
            "text": message.body,
            "officeId": office.id,
            "visitorName": session.visitor_label or "Visitor",
            "message": f"New office message for {session.visitor_label or 'Visitor'}",
        },
    )
    _emit_office_event(
        event_name="office.message.created",
        office=office,
        payload={
            "sessionId": session.id,
            "messageId": message.id,
            "text": message.body,
            "officeId": office.id,
        },
        source="office.message.create",
    )
    return {
        "id": message.id,
        "sessionId": message.session_id,
        "text": message.body,
        "senderType": message.sender_type,
        "displayName": "Office",
        "time": message.created_at.isoformat(),
        "read": True,
    }


def update_office_visitor_status(
    db: Session,
    *,
    user_id: str,
    session_id: str,
    status: str,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == session_id).first()
    if not session:
        raise ValueError("Visitor request not found")
    normalized = str(status or "").strip().lower()
    session.status = normalized
    session.state_updated_at = utc_now()
    if normalized in {"approved", "checked_in"}:
        session.homeowner_decision_at = session.homeowner_decision_at or utc_now()
        session.gate_status = "waiting"
    if normalized in {"rejected", "cancelled", "expired", "checked_out"}:
        session.ended_at = session.ended_at or utc_now()
        session.gate_status = "denied_at_gate"
    db.commit()
    db.refresh(session)
    create_notification(
        db=db,
        user_id=session.homeowner_id,
        kind=f"office.visitor_request.{normalized}",
        payload={
            "sessionId": session.id,
            "officeId": office.id,
            "status": normalized,
            "visitorName": session.visitor_label or "Visitor",
            "message": f"Visitor request {normalized}.",
        },
    )
    _emit_office_event(
        event_name=f"office.visitor_request.{normalized}",
        office=office,
        payload={
            "sessionId": session.id,
            "officeId": office.id,
            "status": normalized,
            "visitorName": session.visitor_label or "Visitor",
            "snapshotUrl": session.snapshot_url or session.photo_url or "",
        },
        source=f"office.visitor_request.{normalized}",
    )
    return _session_visitors_item(session)


def assign_office_visitor_request(
    db: Session,
    *,
    user_id: str,
    session_id: str,
    assignee_name: str | None = None,
    assignee_department: str | None = None,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == session_id).first()
    if not session:
        raise ValueError("Visitor request not found")
    session.preferred_communication_target = (assignee_name or session.preferred_communication_target or "").strip() or None
    session.preferred_communication_channel = "message"
    session.state_updated_at = utc_now()
    db.commit()
    db.refresh(session)
    create_notification(
        db=db,
        user_id=session.homeowner_id,
        kind="office.visitor_request.assigned",
        payload={
            "sessionId": session.id,
            "officeId": office.id,
            "assigneeName": assignee_name or "",
            "department": assignee_department or "",
            "message": f"Visitor request assigned to {assignee_name or 'reception'}.",
        },
    )
    _emit_office_event(
        event_name="office.visitor_request.updated",
        office=office,
        payload={
            "sessionId": session.id,
            "officeId": office.id,
            "assigneeName": assignee_name or "",
            "department": assignee_department or "",
            "status": session.status,
        },
        source="office.visitor_request.assign",
    )
    return _session_visitors_item(session)
