from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.db.models import Door, Home, Message, Notification, User, VisitorSession


def _status_label(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"pending", "forwarded", "forwarded_to_homeowner"}:
        return "Awaiting approval"
    if normalized in {"approved", "active"}:
        return "Access active"
    if normalized in {"rejected"}:
        return "Denied"
    if normalized in {"closed", "completed"}:
        return "Completed"
    return normalized.title() or "Update"


def get_dashboard_overview(db: Session, homeowner_id: str) -> dict:
    user = db.query(User).filter(User.id == homeowner_id).first()
    sessions = (
        db.query(VisitorSession)
        .filter(VisitorSession.homeowner_id == homeowner_id)
        .order_by(VisitorSession.started_at.desc())
        .all()
    )
    doors = (
        db.query(Door, Home)
        .join(Home, Home.id == Door.home_id)
        .filter(Home.homeowner_id == homeowner_id)
        .all()
    )
    door_map = {
        door.id: {
            "name": door.name,
            "gateLabel": door.gate_label or door.name,
            "homeName": home.name,
        }
        for door, home in doors
    }
    pending = [s for s in sessions if s.status == "pending"]
    active = [s for s in sessions if s.status == "active"]

    latest_messages = (
        db.query(Message)
        .join(VisitorSession, Message.session_id == VisitorSession.id)
        .filter(VisitorSession.homeowner_id == homeowner_id)
        .order_by(Message.created_at.desc())
        .limit(5)
        .all()
    )
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == homeowner_id)
        .order_by(Notification.created_at.desc())
        .limit(5)
        .all()
    )
    unread_messages = len([m for m in latest_messages if m.sender_type != "homeowner" and m.read_by_homeowner_at is None])

    activity_items = []
    for session in sessions[:6]:
        door_info = door_map.get(session.door_id, {})
        detail_bits = []
        if session.visitor_label:
            detail_bits.append(f"Visitor: {session.visitor_label}")
        if door_info.get("name"):
            detail_bits.append(f"Door: {door_info['name']}")
        if session.purpose:
            detail_bits.append(session.purpose)
        activity_items.append(
            {
                "id": session.id,
                "event": door_info.get("gateLabel") or f"Visitor at door {session.door_id}",
                "details": " • ".join(detail_bits) or "Visitor activity update",
                "time": session.started_at.isoformat() if session.started_at else "",
                "state": session.status,
            }
        )

    for note in notifications:
        try:
            note_payload = json.loads(note.payload or "{}")
        except Exception:
            note_payload = {}
        activity_items.append(
            {
                "id": note.id,
                "event": str(note.kind or "Notification").replace(".", " ").title(),
                "details": str(note_payload.get("message") or "Resident notification"),
                "time": note.created_at.isoformat() if note.created_at else "",
                "state": "notification",
            }
        )

    activity_items.sort(key=lambda item: item.get("time") or "", reverse=True)

    return {
        "metrics": {
            "activeVisitors": len(active),
            "pendingApprovals": len(pending),
            "callsToday": len([s for s in sessions if s.status in {"active", "approved"}]),
            "unreadMessages": unread_messages,
        },
        "activity": activity_items[:8],
        "waitingRoom": [
            {
                "id": s.id,
                "visitorName": s.visitor_label or "Visitor",
                "door": door_map.get(s.door_id, {}).get("name") or s.door_id,
                "gateLabel": door_map.get(s.door_id, {}).get("gateLabel") or s.door_id,
                "wait": "00:45",
                "status": _status_label(s.status),
                "purpose": s.purpose or "",
            }
            for s in pending[:10]
        ],
        "session": (
            {
                "id": active[0].id,
                "state": active[0].status,
                "location": door_map.get(active[0].door_id, {}).get("gateLabel") or active[0].door_id,
                "duration": "04:19",
            }
            if active
            else None
        ),
        "messages": [
            {
                "id": m.id,
                "from": m.sender_type,
                "text": m.body,
                "time": m.created_at.isoformat(),
                "unread": m.sender_type != "homeowner" and m.read_by_homeowner_at is None,
            }
            for m in latest_messages
        ],
        "profile": {
            "fullName": user.full_name if user else "Homeowner",
            "email": user.email if user else "",
            "phone": user.phone if user else None,
            "primaryDoor": door_map.get(active[0].door_id, {}).get("gateLabel") if active else (next(iter(door_map.values()), {}) or {}).get("gateLabel"),
            "doorCount": len(door_map),
        },
        "traffic": [20, 24, 21, 30, 27, 35, 31],
        "callControls": {
            "canAudio": True,
            "canVideo": True,
            "canMute": True,
            "canEnd": True,
        },
    }
