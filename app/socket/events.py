from collections import defaultdict
from datetime import datetime

from app.core.config import get_settings
from app.core.security import decode_token
from app.db.models import Message, VisitorSession
from app.db.session import SessionLocal
from app.socket.manager import socket_state

settings = get_settings()
session_members: dict[str, set[str]] = defaultdict(set)


def _resolve_user_id(auth: dict | None) -> str | None:
    auth = auth or {}
    if auth.get("userId"):
        return auth["userId"]
    token = auth.get("token")
    if not token:
        return None
    try:
        payload = decode_token(token)
        return payload.get("sub")
    except Exception:
        return None


def register_socket_events(sio):
    @sio.event(namespace=settings.DASHBOARD_NAMESPACE)
    async def connect(sid, environ, auth):
        user_id = _resolve_user_id(auth)
        if user_id:
            socket_state.bind(user_id, sid)
        await sio.emit(
            "dashboard.snapshot",
            {"data": {"message": "connected"}},
            to=sid,
            namespace=settings.DASHBOARD_NAMESPACE,
        )

    @sio.event(namespace=settings.DASHBOARD_NAMESPACE)
    async def disconnect(sid):
        socket_state.unbind_sid(sid)

    @sio.on("dashboard.subscribe", namespace=settings.DASHBOARD_NAMESPACE)
    async def dashboard_subscribe(sid, payload):
        room = (payload or {}).get("room")
        if room:
            await sio.enter_room(sid, room, namespace=settings.DASHBOARD_NAMESPACE)

    @sio.event(namespace=settings.SIGNALING_NAMESPACE)
    async def connect(sid, environ, auth):  # type: ignore[no-redef]
        user_id = _resolve_user_id(auth)
        if user_id:
            socket_state.bind(user_id, sid)

    @sio.event(namespace=settings.SIGNALING_NAMESPACE)
    async def disconnect(sid):  # type: ignore[no-redef]
        socket_state.unbind_sid(sid)
        for room, members in list(session_members.items()):
            if sid in members:
                members.discard(sid)
                await sio.emit(
                    "session.participant_left",
                    {"sid": sid, "count": len(members)},
                    room=room,
                    namespace=settings.SIGNALING_NAMESPACE,
                )
                if not members:
                    session_members.pop(room, None)

    @sio.on("session.join", namespace=settings.SIGNALING_NAMESPACE)
    async def session_join(sid, payload):
        session_id = (payload or {}).get("sessionId")
        if not session_id:
            return
        room = f"session:{session_id}"
        await sio.enter_room(sid, room, namespace=settings.SIGNALING_NAMESPACE)
        session_members[room].add(sid)
        await sio.emit(
            "session.participant_joined",
            {
                "sid": sid,
                "displayName": (payload or {}).get("displayName") or "Participant",
                "count": len(session_members[room]),
            },
            room=room,
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        await sio.emit(
            "session.joined",
            {"sid": sid, "count": len(session_members[room])},
            to=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )

    @sio.on("webrtc.offer", namespace=settings.SIGNALING_NAMESPACE)
    async def webrtc_offer(sid, payload):
        target = (payload or {}).get("target")
        if target:
            await sio.emit("webrtc.offer", payload, room=target, namespace=settings.SIGNALING_NAMESPACE)
            return
        session_id = (payload or {}).get("sessionId")
        if session_id:
            await sio.emit(
                "webrtc.offer",
                {**(payload or {}), "sender": sid},
                room=f"session:{session_id}",
                skip_sid=sid,
                namespace=settings.SIGNALING_NAMESPACE,
            )

    @sio.on("webrtc.answer", namespace=settings.SIGNALING_NAMESPACE)
    async def webrtc_answer(sid, payload):
        target = (payload or {}).get("target")
        if target:
            await sio.emit("webrtc.answer", payload, room=target, namespace=settings.SIGNALING_NAMESPACE)
            return
        session_id = (payload or {}).get("sessionId")
        if session_id:
            await sio.emit(
                "webrtc.answer",
                {**(payload or {}), "sender": sid},
                room=f"session:{session_id}",
                skip_sid=sid,
                namespace=settings.SIGNALING_NAMESPACE,
            )

    @sio.on("webrtc.ice", namespace=settings.SIGNALING_NAMESPACE)
    async def webrtc_ice(sid, payload):
        target = (payload or {}).get("target")
        if target:
            await sio.emit("webrtc.ice", payload, room=target, namespace=settings.SIGNALING_NAMESPACE)
            return
        session_id = (payload or {}).get("sessionId")
        if session_id:
            await sio.emit(
                "webrtc.ice",
                {**(payload or {}), "sender": sid},
                room=f"session:{session_id}",
                skip_sid=sid,
                namespace=settings.SIGNALING_NAMESPACE,
            )

    @sio.on("chat.message", namespace=settings.SIGNALING_NAMESPACE)
    async def chat_message(sid, payload):
        session_id = (payload or {}).get("sessionId")
        body = (payload or {}).get("text")
        if not session_id or not body:
            return
        db = SessionLocal()
        try:
            session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
            if not session:
                return
            sender_user_id = socket_state.sid_user.get(sid)
            sender_type = "homeowner" if sender_user_id and sender_user_id == session.homeowner_id else "visitor"
            message = Message(
                session_id=session_id,
                sender_type=sender_type,
                body=str(body).strip(),
            )
            if not message.body:
                return
            db.add(message)
            db.commit()
            db.refresh(message)
            created_at = message.created_at.isoformat()
            message_id = message.id
        finally:
            db.close()
        await sio.emit(
            "chat.message",
            {
                "id": message_id,
                "sessionId": session_id,
                "text": str(body).strip(),
                "senderType": sender_type,
                "senderSid": sid,
                "displayName": (payload or {}).get("displayName") or "Participant",
                "at": created_at,
            },
            room=f"session:{session_id}",
            namespace=settings.SIGNALING_NAMESPACE,
        )

    @sio.on("session.control", namespace=settings.SIGNALING_NAMESPACE)
    async def session_control(sid, payload):
        session_id = (payload or {}).get("sessionId")
        action = (payload or {}).get("action")
        if not session_id or not action:
            return
        await sio.emit(
            "session.control",
            {
                "sessionId": session_id,
                "action": action,
                "senderSid": sid,
                "at": datetime.utcnow().isoformat(),
            },
            room=f"session:{session_id}",
            namespace=settings.SIGNALING_NAMESPACE,
        )
