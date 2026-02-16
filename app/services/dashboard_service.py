from sqlalchemy.orm import Session

from app.db.models import Message, VisitorSession


def get_dashboard_overview(db: Session, homeowner_id: str) -> dict:
    sessions = db.query(VisitorSession).filter(VisitorSession.homeowner_id == homeowner_id).all()
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

    return {
        "metrics": {
            "activeVisitors": len(active),
            "pendingApprovals": len(pending),
            "callsToday": len([s for s in sessions if s.status in {"active", "approved"}]),
            "unreadMessages": len(latest_messages),
        },
        "activity": [
            {
                "id": s.id,
                "event": f"Visitor at door {s.door_id}",
                "time": s.started_at.isoformat(),
                "state": s.status,
            }
            for s in sessions[-10:]
        ],
        "waitingRoom": [
            {"id": s.id[:6], "door": s.door_id, "wait": "00:45", "status": "Awaiting approval"}
            for s in pending[:10]
        ],
        "session": (
            {
                "id": active[0].id,
                "state": active[0].status,
                "location": active[0].door_id,
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
            }
            for m in latest_messages
        ],
        "traffic": [20, 24, 21, 30, 27, 35, 31],
        "callControls": {
            "canAudio": True,
            "canVideo": True,
            "canMute": True,
            "canEnd": True,
        },
    }
