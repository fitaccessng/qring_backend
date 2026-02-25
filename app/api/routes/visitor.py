import logging
from time import perf_counter

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.schemas.visitor import VisitorRequestCreate
from app.services.notification_service import create_notification
from app.services.qr_service import resolve_qr
from app.services.session_service import create_visitor_session
from app.socket.server import sio

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)


@router.post("/request")
async def visitor_request(payload: VisitorRequestCreate, db: Session = Depends(get_db)):
    started = perf_counter()
    phase = "resolve_qr"
    session = None
    try:
        qr = resolve_qr(db, payload.qrId)
        phase = "create_session"
        session = create_visitor_session(
            db=db,
            qr_id=payload.qrId,
            qr_home_id=qr["home_id"],
            doors=qr["doors"],
            mode=qr["mode"],
            requested_door=payload.doorId,
            visitor_label=(payload.name or "Visitor").strip() or "Visitor",
        )

        phase = "create_notification"
        create_notification(
            db=db,
            user_id=session.homeowner_id,
            kind="visitor.request",
            payload={
                "sessionId": session.id,
                "doorId": session.door_id,
                "visitorName": (payload.name or "Visitor").strip() or "Visitor",
                "purpose": (payload.purpose or "").strip(),
                "message": f"New visitor request from {(payload.name or 'Visitor').strip() or 'Visitor'}",
            },
        )

        phase = "emit_dashboard_patch"
        await sio.emit(
            "dashboard.patch",
            {
                "data": {
                    "activity": [
                        {
                            "id": session.id,
                            "event": f"Visitor request at door {session.door_id}",
                            "time": session.started_at.isoformat(),
                            "state": "pending",
                        }
                    ]
                }
            },
            namespace=settings.DASHBOARD_NAMESPACE,
        )

        elapsed_ms = (perf_counter() - started) * 1000
        logger.info(
            "visitor.request completed in %.1fms phase=%s qr_id=%s session_id=%s",
            elapsed_ms,
            phase,
            payload.qrId,
            session.id,
        )
        return {"data": {"sessionId": session.id, "status": session.status}}
    except Exception:
        elapsed_ms = (perf_counter() - started) * 1000
        logger.exception(
            "visitor.request failed in %.1fms phase=%s qr_id=%s door_id=%s",
            elapsed_ms,
            phase,
            payload.qrId,
            payload.doorId,
        )
        raise


@router.get("/sessions/{session_id}")
def visitor_session_status(session_id: str, db: Session = Depends(get_db)):
    from app.db.models import VisitorSession
    row = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not row:
        return {"data": {"sessionId": session_id, "status": "not_found"}}
    return {
        "data": {
            "sessionId": row.id,
            "status": row.status,
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "endedAt": row.ended_at.isoformat() if row.ended_at else None,
        }
    }


@router.get("/sessions/{session_id}/messages")
def visitor_session_messages(session_id: str, db: Session = Depends(get_db)):
    from app.db.models import Message, VisitorSession

    session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
    if not session:
        return {"data": []}

    rows = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return {
        "data": [
            {
                "id": row.id,
                "sessionId": row.session_id,
                "text": row.body,
                "senderType": row.sender_type,
                "displayName": "Homeowner" if row.sender_type == "homeowner" else "Visitor",
                "at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }
