from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import AuditLog, CallSession, Door, Estate, EstateAlert, GateLog, Home, HomeownerSetting, Message, Notification, User, UserRole, VisitorSession
from app.services.notification_service import create_notification

OPEN_SECURITY_STATUSES = {"submitted", "received_by_security", "forwarded_to_homeowner", "approved"}
STATE_FLOW = {
    "submitted": {"received_by_security"},
    "received_by_security": {"forwarded_to_homeowner", "approved", "rejected"},
    "forwarded_to_homeowner": {"approved", "rejected"},
    "approved": {"gate_confirmed", "completed"},
    "rejected": {"completed"},
    "gate_confirmed": {"completed"},
    "completed": set(),
}


def _visitor_key(session: VisitorSession) -> tuple[str, str]:
    phone = (session.visitor_phone or "").strip()
    if phone:
        return ("phone", phone)
    name = (session.visitor_label or "visitor").strip().lower()
    return ("name", name)


def _safe_json_loads(raw: str | None, default):
    try:
        return json.loads(raw or "")
    except Exception:
        return default


def compute_session_trust(db: Session, session: VisitorSession) -> dict[str, Any]:
    match_field, match_value = _visitor_key(session)
    query = db.query(VisitorSession).filter(VisitorSession.id != session.id)
    if match_field == "phone":
        query = query.filter(VisitorSession.visitor_phone == match_value)
    else:
        query = query.filter(func.lower(VisitorSession.visitor_label) == match_value)
    rows = query.all()
    total_visits = len(rows)
    approvals_count = len([row for row in rows if row.status in {"approved", "gate_confirmed", "completed"}])
    rejections_count = len([row for row in rows if row.status == "rejected" or row.gate_status == "denied_at_gate"])
    unique_houses_visited = len({row.home_id for row in rows if row.home_id})
    repeat_visits_to_home = len([row for row in rows if row.home_id == session.home_id and row.status in {"approved", "gate_confirmed", "completed"}])
    trust_score = max(0, approvals_count * 20 + repeat_visits_to_home * 10 - rejections_count * 25 - max(unique_houses_visited - 3, 0) * 5)
    if rejections_count >= 2:
        status = "flagged"
    elif approvals_count >= 3 or repeat_visits_to_home >= 2:
        status = "trusted"
    else:
        status = "new"
    return {
        "total_visits": total_visits,
        "approvals_count": approvals_count,
        "rejections_count": rejections_count,
        "unique_houses_visited": unique_houses_visited,
        "repeat_visits_to_home": repeat_visits_to_home,
        "trust_score": trust_score,
        "trust_status": status,
        "auto_approve_suggested": repeat_visits_to_home >= 2 and rejections_count == 0,
    }


def detect_suspicious_pattern(db: Session, session: VisitorSession, estate: Estate | None) -> tuple[bool, str | None]:
    if not estate:
        return False, None
    match_field, match_value = _visitor_key(session)
    query = db.query(VisitorSession).filter(VisitorSession.id != session.id, VisitorSession.estate_id == session.estate_id)
    if match_field == "phone":
        query = query.filter(VisitorSession.visitor_phone == match_value)
    else:
        query = query.filter(func.lower(VisitorSession.visitor_label) == match_value)
    window_start = datetime.utcnow() - timedelta(minutes=max(int(estate.suspicious_visit_window_minutes or 20), 5))
    rows = query.filter(VisitorSession.started_at >= window_start).all()
    unique_houses = len({row.home_id for row in rows if row.home_id})
    rejection_count = len([row for row in rows if row.status == "rejected"])
    if unique_houses >= int(estate.suspicious_house_threshold or 3):
        return True, "Visitor tried multiple houses in a short time"
    if rejection_count >= int(estate.suspicious_rejection_threshold or 2):
        return True, "Visitor was rejected repeatedly"
    return False, None


def evaluate_session_intelligence(
    db: Session,
    session: VisitorSession,
    *,
    estate: Estate | None = None,
    homeowner_settings: HomeownerSetting | None = None,
) -> VisitorSession:
    trust = compute_session_trust(db, session)
    session.trust_status = trust["trust_status"]
    session.trust_score = trust["trust_score"]
    session.total_visits_snapshot = trust["total_visits"]
    session.approvals_count_snapshot = trust["approvals_count"]
    session.rejections_count_snapshot = trust["rejections_count"]
    session.unique_houses_visited_snapshot = trust["unique_houses_visited"]
    session.repeat_visits_to_home_snapshot = trust["repeat_visits_to_home"]
    session.auto_approve_suggested = trust["auto_approve_suggested"]
    if session.visitor_type == "delivery":
        session.delivery_drop_off_allowed = bool(getattr(homeowner_settings, "allow_delivery_drop_at_gate", True))
    flagged, reason = detect_suspicious_pattern(db, session, estate)
    session.suspicious_flag = flagged
    session.suspicious_reason = reason
    can_auto_approve = False
    if homeowner_settings and homeowner_settings.auto_approve_trusted_visitors and session.trust_status == "trusted":
        can_auto_approve = True
        session.pre_approved_reason = "Trusted visitor auto-approval"
    elif estate and estate.auto_approve_trusted_visitors and session.trust_status == "trusted":
        can_auto_approve = True
        session.pre_approved_reason = "Estate trusted-visitor rule"
    session.auto_approved = can_auto_approve and not flagged and session.visitor_type != "delivery"
    return session


def _create_gate_log(
    db: Session,
    *,
    session: VisitorSession,
    actor: User | None,
    action: str,
    notes: str | None = None,
) -> None:
    db.add(
        GateLog(
            visitor_session_id=session.id,
            estate_id=session.estate_id,
            home_id=session.home_id,
            gate_id=session.gate_id,
            actor_user_id=actor.id if actor else None,
            actor_role=actor.role.value if actor else None,
            action=action,
            resulting_status=session.status,
            notes=notes,
            meta_json=json.dumps(
                {
                    "gateStatus": session.gate_status,
                    "communicationStatus": session.communication_status,
                    "trustStatus": session.trust_status,
                }
            ),
        )
    )
    db.add(
        AuditLog(
            actor_user_id=actor.id if actor else None,
            action=action,
            resource_type="visitor_session",
            resource_id=session.id,
            meta_json=json.dumps({"status": session.status, "gateStatus": session.gate_status}),
        )
    )


def _transition_session(session: VisitorSession, next_state: str, *, now: datetime) -> None:
    current = (session.status or "submitted").strip()
    if next_state not in STATE_FLOW.get(current, set()) and next_state != current:
        raise AppException(f"Cannot move request from {current} to {next_state}", status_code=400)
    session.status = next_state
    session.state_updated_at = now
    if next_state == "received_by_security":
        session.received_by_security_at = session.received_by_security_at or now
    if next_state == "forwarded_to_homeowner":
        session.forwarded_to_homeowner_at = session.forwarded_to_homeowner_at or now
    if next_state in {"approved", "rejected"}:
        session.homeowner_decision_at = session.homeowner_decision_at or now
    if next_state in {"gate_confirmed", "completed"}:
        session.gate_action_at = session.gate_action_at or now
    if next_state == "completed":
        session.ended_at = session.ended_at or now


def get_estate_security_rules(db: Session, estate_id: str) -> dict[str, Any]:
    estate = db.query(Estate).filter(Estate.id == estate_id).first()
    if not estate:
        return {
            "canApproveWithoutHomeowner": False,
            "mustNotifyHomeowner": True,
            "requirePhotoVerification": False,
            "requireCallBeforeApproval": False,
            "autoApproveTrustedVisitors": False,
        }
    return {
        "canApproveWithoutHomeowner": bool(estate.security_can_approve_without_homeowner),
        "mustNotifyHomeowner": bool(estate.security_must_notify_homeowner),
        "requirePhotoVerification": bool(estate.security_require_photo_verification),
        "requireCallBeforeApproval": bool(estate.security_require_call_before_approval),
        "autoApproveTrustedVisitors": bool(estate.auto_approve_trusted_visitors),
    }


def list_security_accounts_for_estate(db: Session, estate_id: str, gate_id: str | None = None) -> list[User]:
    query = db.query(User).filter(User.role == UserRole.security, User.estate_id == estate_id, User.is_active.is_(True))
    if gate_id:
        targeted = query.filter((User.gate_id == gate_id) | (User.gate_id.is_(None))).order_by(User.full_name.asc()).all()
        if targeted:
            return targeted
    return query.order_by(User.full_name.asc()).all()


def _route_targets_for_session(db: Session, session: VisitorSession) -> tuple[Home | None, Estate | None, list[User]]:
    door = db.query(Door).filter(Door.id == session.door_id).first()
    home = db.query(Home).filter(Home.id == session.home_id).first() if session.home_id else None
    estate = db.query(Estate).filter(Estate.id == (session.estate_id or (home.estate_id if home else None))).first()
    gate_id = session.gate_id or (door.gate_label if door else None)
    security_users = list_security_accounts_for_estate(db, estate.id, gate_id=gate_id) if estate else []
    return home, estate, security_users


def serialize_security_session(db: Session, session: VisitorSession) -> dict[str, Any]:
    door = db.query(Door).filter(Door.id == session.door_id).first()
    home = db.query(Home).filter(Home.id == session.home_id).first()
    estate = db.query(Estate).filter(Estate.id == (session.estate_id or (home.estate_id if home else None))).first()
    security_user = db.query(User).filter(User.id == session.handled_by_security_id).first() if session.handled_by_security_id else None
    request_source = (session.request_source or "").strip().lower()
    if request_source not in {"visitor_qr", "gateman_assisted"}:
        request_source = "gateman_assisted" if str(session.qr_id or "").startswith("security-manual:") else "visitor_qr"
    creator_role = (session.creator_role or "").strip().lower()
    if creator_role not in {"visitor", "security"}:
        creator_role = "security" if request_source == "gateman_assisted" else "visitor"
    return {
        "id": session.id,
        "visitorName": session.visitor_label or "Visitor",
        "visitorPhone": session.visitor_phone,
        "purpose": session.purpose or "",
        "photoUrl": session.photo_url,
        "doorId": session.door_id,
        "doorName": door.name if door else "",
        "gateId": session.gate_id or (door.gate_label if door else None),
        "gateLabel": session.gate_id or (door.gate_label if door else "Main Gate"),
        "homeId": session.home_id,
        "homeName": home.name if home else "",
        "homeownerId": session.homeowner_id,
        "estateId": estate.id if estate else session.estate_id,
        "estateName": estate.name if estate else "",
        "status": session.status,
        "communicationStatus": session.communication_status or "none",
        "preferredCommunicationChannel": session.preferred_communication_channel,
        "preferredCommunicationTarget": session.preferred_communication_target,
        "gateStatus": session.gate_status or "waiting",
        "creatorRole": creator_role,
        "requestSource": request_source,
        "visitorType": session.visitor_type or "guest",
        "trustStatus": session.trust_status or "new",
        "trustScore": int(session.trust_score or 0),
        "trustMetrics": {
            "totalVisits": int(session.total_visits_snapshot or 0),
            "approvals": int(session.approvals_count_snapshot or 0),
            "rejections": int(session.rejections_count_snapshot or 0),
            "uniqueHousesVisited": int(session.unique_houses_visited_snapshot or 0),
            "repeatVisitsToHome": int(session.repeat_visits_to_home_snapshot or 0),
        },
        "autoApproved": bool(session.auto_approved),
        "autoApproveSuggested": bool(session.auto_approve_suggested),
        "preApproved": bool(session.pre_approved),
        "preApprovedReason": session.pre_approved_reason,
        "deliveryOption": session.delivery_option,
        "deliveryDropOffAllowed": bool(session.delivery_drop_off_allowed),
        "suspiciousFlag": bool(session.suspicious_flag),
        "suspiciousReason": session.suspicious_reason,
        "handledBySecurityId": session.handled_by_security_id,
        "handledBySecurityName": security_user.full_name if security_user else None,
        "receivedBySecurityAt": session.received_by_security_at.isoformat() if session.received_by_security_at else None,
        "forwardedToHomeownerAt": session.forwarded_to_homeowner_at.isoformat() if session.forwarded_to_homeowner_at else None,
        "homeownerDecisionAt": session.homeowner_decision_at.isoformat() if session.homeowner_decision_at else None,
        "gateActionAt": session.gate_action_at.isoformat() if session.gate_action_at else None,
        "stateUpdatedAt": session.state_updated_at.isoformat() if session.state_updated_at else None,
        "startedAt": session.started_at.isoformat() if session.started_at else None,
        "endedAt": session.ended_at.isoformat() if session.ended_at else None,
        "waitingSeconds": max(0, int((datetime.utcnow() - (session.started_at or datetime.utcnow())).total_seconds())),
    }


def _get_security_user(db: Session, security_user_id: str) -> User:
    user = db.query(User).filter(User.id == security_user_id, User.role == UserRole.security).first()
    if not user:
        raise AppException("Security account not found", status_code=404)
    return user


def _security_session_query(db: Session, security_user: User):
    query = db.query(VisitorSession).filter(VisitorSession.estate_id == security_user.estate_id)
    if security_user.gate_id:
        query = query.filter((VisitorSession.gate_id == security_user.gate_id) | (VisitorSession.gate_id.is_(None)))
    return query


def get_security_dashboard(db: Session, security_user_id: str) -> dict[str, Any]:
    security_user = _get_security_user(db, security_user_id)
    if not security_user.estate_id:
        return {"profile": {"id": security_user.id, "fullName": security_user.full_name}, "queues": {}}

    sessions = _security_session_query(db, security_user).order_by(VisitorSession.started_at.desc()).limit(120).all()
    serialized = [serialize_security_session(db, session) for session in sessions]
    suspicious_alerts = [
        {
            "id": row.id,
            "visitorName": row.visitor_label or "Visitor",
            "reason": row.suspicious_reason or "Suspicious activity detected",
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "status": row.status,
        }
        for row in sessions
        if row.suspicious_flag
    ][:6]
    delivery_count = len([row for row in sessions if row.visitor_type == "delivery" and row.status not in {"completed", "rejected"}])

    return {
        "profile": {
            "id": security_user.id,
            "fullName": security_user.full_name,
            "email": security_user.email,
            "phone": security_user.phone,
            "estateId": security_user.estate_id,
            "gateId": security_user.gate_id,
        },
        "queues": {
            "newRequests": [row for row in serialized if row["status"] == "submitted"],
            "waitingForHomeowner": [row for row in serialized if row["status"] in {"received_by_security", "forwarded_to_homeowner"}],
            "approvedPendingEntry": [row for row in serialized if row["status"] == "approved" and row["gateStatus"] == "waiting"],
            "completed": [row for row in serialized if row["status"] in {"completed", "rejected", "closed"} or row["gateStatus"] in {"allowed_in", "denied_at_gate"}],
        },
        "rules": get_estate_security_rules(db, security_user.estate_id),
        "alerts": suspicious_alerts,
        "summary": {
            "deliveryWaiting": delivery_count,
            "flaggedVisitors": len(suspicious_alerts),
        },
    }


def notify_security_request(db: Session, session: VisitorSession) -> None:
    home, estate, security_users = _route_targets_for_session(db, session)
    if not estate:
        return
    if not security_users:
        return

    for security_user in security_users:
        create_notification(
            db=db,
            user_id=security_user.id,
            kind="security.visitor_request",
            payload={
                "sessionId": session.id,
                "doorId": session.door_id,
                "doorName": home.name if home else "",
                "visitorName": session.visitor_label or "Visitor",
                "phoneNumber": session.visitor_phone or "",
                "purpose": session.purpose or "",
                "photoUrl": session.photo_url,
                "estateId": estate.id,
                "homeownerId": session.homeowner_id,
                "message": f"Visitor awaiting security review for {session.visitor_label or 'Visitor'}",
            },
        )


def update_security_session_status(
    db: Session,
    *,
    session_id: str,
    actor: User,
    action: str,
    preferred_communication_channel: str | None = None,
    preferred_communication_target: str | None = None,
) -> VisitorSession:
    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        raise AppException("Visitor request not found", status_code=404)

    action = (action or "").strip().lower()
    now = datetime.utcnow()
    normalized_channel = (preferred_communication_channel or "").strip().lower() or None
    if normalized_channel not in {None, "message", "audio", "video"}:
        raise AppException("Preferred communication channel is invalid.", status_code=400)
    normalized_target = (preferred_communication_target or "").strip().lower() or None
    if normalized_target not in {None, "visitor", "gateman"}:
        raise AppException("Preferred communication target is invalid.", status_code=400)
    home, estate, _ = _route_targets_for_session(db, session)
    rules = get_estate_security_rules(db, estate.id if estate else "")
    homeowner_settings = (
        db.query(HomeownerSetting).filter(HomeownerSetting.user_id == session.homeowner_id).first()
        if session.homeowner_id
        else None
    )
    evaluate_session_intelligence(db, session, estate=estate, homeowner_settings=homeowner_settings)

    if actor.role == UserRole.security:
        if actor.estate_id and session.estate_id != actor.estate_id:
            raise AppException("You cannot manage requests outside your estate", status_code=403)
        if session.status == "submitted":
            _transition_session(session, "received_by_security", now=now)
            session.handled_by_security_id = actor.id
        if action == "forward":
            _transition_session(session, "forwarded_to_homeowner", now=now)
            session.handled_by_security_id = actor.id
            _create_gate_log(db, session=session, actor=actor, action="security_forwarded_to_homeowner")
            db.commit()
            create_notification(
                db=db,
                user_id=session.homeowner_id,
                kind="visitor.forwarded",
                payload={
                    "sessionId": session.id,
                    "visitorName": session.visitor_label or "Visitor",
                    "doorId": session.door_id,
                    "purpose": session.purpose or "",
                    "message": f"Security forwarded {session.visitor_label or 'a visitor'} for your decision.",
                },
            )
            db.refresh(session)
            return session
        if action == "approve_repeat_visitor":
            if not session.auto_approve_suggested and session.trust_status != "trusted":
                raise AppException("This visitor is not yet eligible for repeat approval.", status_code=400)
            action = "approve"
        if action == "delivery_drop_off":
            if session.visitor_type != "delivery":
                raise AppException("Delivery drop-off is only available for delivery visitors.", status_code=400)
            _transition_session(session, "approved", now=now)
            session.delivery_option = "drop_at_gate"
            session.delivery_drop_off_allowed = True
            session.gate_status = "waiting"
            session.handled_by_security_id = actor.id
            session.pre_approved = True
            session.pre_approved_reason = "Delivery drop-off mode"
            _create_gate_log(db, session=session, actor=actor, action="security_delivery_drop_off")
            db.commit()
            db.refresh(session)
            return session
        if action in {"approve", "reject"}:
            if action == "approve" and not rules["canApproveWithoutHomeowner"]:
                raise AppException("Estate rules require homeowner approval first", status_code=403)
            if action == "approve" and rules["requireCallBeforeApproval"]:
                existing_call = (
                    db.query(CallSession)
                    .filter(CallSession.visitor_session_id == session.id, CallSession.status.in_(["ongoing", "ended"]))
                    .first()
                )
                if not existing_call:
                    raise AppException("Estate rules require a call before approval", status_code=403)
            _transition_session(session, "approved" if action == "approve" else "rejected", now=now)
            session.handled_by_security_id = actor.id
            session.gate_status = "waiting" if action == "approve" else "denied_at_gate"
            if action == "reject":
                session.ended_at = now
            _create_gate_log(db, session=session, actor=actor, action=f"security_{action}")
            db.commit()
            if rules["mustNotifyHomeowner"]:
                create_notification(
                    db=db,
                    user_id=session.homeowner_id,
                    kind="security.decision",
                    payload={
                        "sessionId": session.id,
                        "visitorName": session.visitor_label or "Visitor",
                        "decision": session.status,
                        "message": f"Security {session.status} {session.visitor_label or 'a visitor'} at the gate.",
                    },
                )
            db.refresh(session)
            return session
        if action in {"confirm_entry", "deny_gate"}:
            session.gate_status = "allowed_in" if action == "confirm_entry" else "denied_at_gate"
            _transition_session(session, "gate_confirmed" if action == "confirm_entry" else "rejected", now=now)
            session.handled_by_security_id = actor.id
            if action == "confirm_entry":
                _transition_session(session, "completed", now=now)
            else:
                session.ended_at = now
            _create_gate_log(db, session=session, actor=actor, action=f"security_{action}")
            db.commit()
            db.refresh(session)
            return session
    elif actor.role == UserRole.homeowner:
        if session.homeowner_id != actor.id:
            raise AppException("You cannot decide this visitor request", status_code=403)
        if session.status == "submitted":
            _transition_session(session, "received_by_security", now=now)
        if action in {"approve", "reject"}:
            if normalized_channel:
                session.preferred_communication_channel = normalized_channel
            if normalized_target:
                session.preferred_communication_target = normalized_target
            if session.status == "received_by_security":
                _transition_session(session, "forwarded_to_homeowner", now=now)
            _transition_session(session, "approved" if action == "approve" else "rejected", now=now)
            if action == "reject":
                session.gate_status = "denied_at_gate"
                session.ended_at = now
            _create_gate_log(db, session=session, actor=actor, action=f"homeowner_{action}")
            db.commit()
            if session.handled_by_security_id:
                create_notification(
                    db=db,
                    user_id=session.handled_by_security_id,
                    kind="homeowner.decision",
                    payload={
                        "sessionId": session.id,
                        "visitorName": session.visitor_label or "Visitor",
                        "decision": session.status,
                        "message": f"Homeowner {session.status} {session.visitor_label or 'the visitor'}.",
                    },
                )
            db.refresh(session)
            return session

    raise AppException("Unsupported action for this user", status_code=400)


def list_security_message_threads(db: Session, security_user_id: str, limit: int = 60) -> list[dict[str, Any]]:
    security_user = _get_security_user(db, security_user_id)
    sessions = _security_session_query(db, security_user).order_by(VisitorSession.started_at.desc()).limit(limit).all()
    if not sessions:
        return []

    session_ids = [row.id for row in sessions]
    messages = (
        db.query(Message)
        .filter(Message.session_id.in_(session_ids))
        .order_by(Message.created_at.desc())
        .all()
    )
    latest_by_session: dict[str, Message] = {}
    unread_by_session: dict[str, int] = {}
    for row in messages:
        if row.session_id not in latest_by_session:
            latest_by_session[row.session_id] = row
        if row.sender_type != "security" and row.read_by_security_at is None:
            unread_by_session[row.session_id] = unread_by_session.get(row.session_id, 0) + 1

    return [
        {
            "id": session.id,
            "name": session.visitor_label or "Visitor",
            "door": serialize_security_session(db, session).get("doorName") or "Door",
            "last": latest_by_session[session.id].body if session.id in latest_by_session else (session.purpose or "Security conversation"),
            "unread": unread_by_session.get(session.id, 0),
            "time": (
                latest_by_session[session.id].created_at.isoformat()
                if session.id in latest_by_session
                else session.started_at.isoformat()
            ),
            "sessionStatus": session.status,
            "gateStatus": session.gate_status,
        }
        for session in sessions
    ]


def list_security_session_messages(
    db: Session,
    *,
    security_user_id: str,
    session_id: str,
    limit: int = 300,
) -> list[dict[str, Any]]:
    security_user = _get_security_user(db, security_user_id)
    session = _security_session_query(db, security_user).filter(VisitorSession.id == session_id).first()
    if not session:
        return []

    db.query(Message).filter(
        Message.session_id == session_id,
        Message.sender_type != "security",
        Message.read_by_security_at.is_(None),
    ).update(
        {Message.read_by_security_at: datetime.utcnow()},
        synchronize_session=False,
    )
    db.commit()

    rows = db.query(Message).filter(Message.session_id == session_id).order_by(Message.created_at.asc()).limit(limit).all()
    security_user_obj = _get_security_user(db, security_user_id)
    return [
        {
            "id": row.id,
            "sessionId": row.session_id,
            "text": row.body,
            "senderType": row.sender_type,
            "displayName": (
                security_user_obj.full_name
                if row.sender_type == "security"
                else "Homeowner" if row.sender_type == "homeowner" else (session.visitor_label or "Visitor")
            ),
            "at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def create_security_session_message(
    db: Session,
    *,
    security_user_id: str,
    session_id: str,
    text: str,
) -> dict[str, Any] | None:
    security_user = _get_security_user(db, security_user_id)
    session = _security_session_query(db, security_user).filter(VisitorSession.id == session_id).first()
    if not session:
        return None
    body = (text or "").strip()
    if not body:
        return None

    session.communication_status = "chatting"
    message = Message(
        session_id=session_id,
        sender_type="security",
        sender_id=security_user.id,
        receiver_id=session.homeowner_id,
        body=body,
        created_at=datetime.utcnow(),
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return {
        "id": message.id,
        "sessionId": message.session_id,
        "text": message.body,
        "senderType": message.sender_type,
        "displayName": security_user.full_name or "Security",
        "at": message.created_at.isoformat(),
    }


def delete_security_session_message(
    db: Session,
    *,
    security_user_id: str,
    session_id: str,
    message_id: str,
) -> bool:
    security_user = _get_security_user(db, security_user_id)
    session = _security_session_query(db, security_user).filter(VisitorSession.id == session_id).first()
    if not session:
        return False
    row = (
        db.query(Message)
        .filter(Message.id == message_id, Message.session_id == session_id, Message.sender_type == "security")
        .first()
    )
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True
