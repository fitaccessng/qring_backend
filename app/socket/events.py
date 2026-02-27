import asyncio
import uuid
from collections import defaultdict
from datetime import datetime

from app.core.config import get_settings
from app.core.security import decode_token
from app.db.models import Message, VisitorSession
from app.db.session import SessionLocal
from app.socket.manager import socket_state

settings = get_settings()
session_members: dict[str, set[str]] = defaultdict(set)
CHAT_PERSIST_RETRY_DELAYS = (0.35, 1.0, 2.0)


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
    async def persist_chat_message_with_retry(
        *,
        sid: str,
        session_id: str,
        message_id: str,
        body: str,
        sender_user_id: str | None,
        optimistic_sender_type: str,
        display_name: str,
        created_at_iso: str,
        client_id: str | None,
    ):
        last_error = "unknown_error"

        for attempt, delay in enumerate(CHAT_PERSIST_RETRY_DELAYS, start=1):
            db = SessionLocal()
            try:
                session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
                if not session:
                    last_error = "session_not_found"
                    break

                resolved_sender_type = (
                    "homeowner" if sender_user_id and sender_user_id == session.homeowner_id else "visitor"
                )
                message = Message(
                    id=message_id,
                    session_id=session_id,
                    sender_type=resolved_sender_type,
                    body=body,
                    created_at=datetime.fromisoformat(created_at_iso),
                )
                db.add(message)
                db.commit()

                await sio.emit(
                    "chat.persisted",
                    {
                        "id": message_id,
                        "sessionId": session_id,
                        "clientId": client_id,
                        "senderType": resolved_sender_type,
                        "displayName": display_name,
                        "text": body,
                        "at": created_at_iso,
                        "persisted": True,
                    },
                    room=f"session:{session_id}",
                    namespace=settings.SIGNALING_NAMESPACE,
                )
                return
            except Exception as exc:
                last_error = str(exc)
            finally:
                db.close()

            if attempt < len(CHAT_PERSIST_RETRY_DELAYS):
                await asyncio.sleep(delay)

        await sio.emit(
            "chat.persist_failed",
            {
                "id": message_id,
                "sessionId": session_id,
                "clientId": client_id,
                "senderType": optimistic_sender_type,
                "displayName": display_name,
                "text": body,
                "at": created_at_iso,
                "persisted": False,
                "error": last_error,
            },
            to=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )

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
        body = str((payload or {}).get("text") or "").strip()
        client_id = (payload or {}).get("clientId")
        if not session_id or not body:
            return
        sender_user_id = socket_state.sid_user.get(sid)
        raw_sender_type = (payload or {}).get("senderType")
        optimistic_sender_type = raw_sender_type if raw_sender_type in {"homeowner", "visitor"} else "visitor"
        display_name = (payload or {}).get("displayName") or "Participant"
        created_at = datetime.utcnow().isoformat()
        message_id = str(uuid.uuid4())

        await sio.emit(
            "chat.message",
            {
                "id": message_id,
                "sessionId": session_id,
                "text": body,
                "clientId": client_id,
                "senderType": optimistic_sender_type,
                "senderSid": sid,
                "displayName": display_name,
                "at": created_at,
                "persisted": False,
            },
            room=f"session:{session_id}",
            namespace=settings.SIGNALING_NAMESPACE,
        )
        await sio.emit(
            "chat.ack",
            {
                "id": message_id,
                "sessionId": session_id,
                "clientId": client_id,
                "at": created_at,
                "status": "queued",
            },
            to=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        asyncio.create_task(
            persist_chat_message_with_retry(
                sid=sid,
                session_id=session_id,
                message_id=message_id,
                body=body,
                sender_user_id=sender_user_id,
                optimistic_sender_type=optimistic_sender_type,
                display_name=display_name,
                created_at_iso=created_at,
                client_id=client_id,
            )
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
