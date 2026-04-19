from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from datetime import datetime

from app.core.config import get_settings
from app.core.security import decode_token
from app.db.models import Estate, ResidentSetting, Message, User, UserRole, VisitorSession
from app.db.session import SessionLocal
from app.socket.manager import socket_state
from app.services.visitor_session_auth import require_visitor_session_access

settings = get_settings()
session_members: dict[str, set[str]] = defaultdict(set)
sid_allowed_sessions: dict[str, set[str]] = defaultdict(set)
CHAT_PERSIST_RETRY_DELAYS = (0.35, 1.0, 2.0)


def _resolve_user_id(auth: dict | None) -> tuple[str | None, str | None]:
    auth = auth or {}
    if auth.get("userId"):
        return auth["userId"], None
    token = auth.get("token")
    if not token:
        return None, None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None, None
        return payload.get("sub"), payload.get("role")
    except Exception:
        return None, None


def _is_user_allowed_for_session(db, *, user: User, session: VisitorSession) -> bool:
    if user.role == UserRole.admin:
        return True
    if user.role == UserRole.homeowner:
        return session.homeowner_id == user.id
    if user.role == UserRole.security:
        return bool(user.estate_id) and bool(session.estate_id) and user.estate_id == session.estate_id
    if user.role == UserRole.estate:
        if not session.estate_id:
            return False
        estate = db.query(Estate).filter(Estate.id == session.estate_id, Estate.owner_id == user.id).first()
        return bool(estate)
    return False


def _normalize_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-11:] if digits else ""


def _user_matches_known_contact(user: User, line: str) -> bool:
    line = str(line or "").strip()
    if not line:
        return False
    normalized_line = line.lower()
    return bool(
        (user.email and user.email.strip().lower() in normalized_line)
        or (_normalize_phone(user.phone) and _normalize_phone(user.phone) in _normalize_phone(line))
        or (_normalize_name(user.full_name) and _normalize_name(user.full_name) in _normalize_name(line))
    )


def _contact_resident_ids_for_user(db, *, user: User) -> list[str]:
    rows = db.query(ResidentSetting).all()
    resident_ids: list[str] = []
    for row in rows:
        try:
            known_contacts = json.loads(row.known_contacts_json or "[]")
        except Exception:
            known_contacts = []
        if any(_user_matches_known_contact(user, item) for item in known_contacts):
            resident_ids.append(row.user_id)
    return resident_ids


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

                resolved_sender_type = "visitor"
                if sender_user_id:
                    if sender_user_id == session.homeowner_id:
                        resolved_sender_type = "homeowner"
                    else:
                        from app.db.models import User

                        sender_user = db.query(User).filter(User.id == sender_user_id).first()
                        if sender_user and sender_user.role.value == "security":
                            resolved_sender_type = "security"
                message = Message(
                    id=message_id,
                    session_id=session_id,
                    sender_type=resolved_sender_type,
                    sender_id=sender_user_id,
                    receiver_id=session.homeowner_id if resolved_sender_type != "homeowner" else None,
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
        user_id, _role = _resolve_user_id(auth)
        if user_id:
            socket_state.bind(user_id, sid)
            await sio.enter_room(sid, f"user:{user_id}", namespace=settings.DASHBOARD_NAMESPACE)
            await sio.enter_room(sid, f"user_{user_id}", namespace=settings.DASHBOARD_NAMESPACE)
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    estate_id = user.estate_id
                    if not estate_id and user.role == UserRole.estate:
                        estate = db.query(Estate).filter(Estate.owner_id == user.id).order_by(Estate.created_at.desc()).first()
                        estate_id = estate.id if estate else None
                    if estate_id:
                        await sio.enter_room(sid, f"estate_{estate_id}", namespace=settings.DASHBOARD_NAMESPACE)
                        await sio.enter_room(sid, f"estate:{estate_id}:panic", namespace=settings.DASHBOARD_NAMESPACE)
                    for resident_id in _contact_resident_ids_for_user(db, user=user):
                        await sio.enter_room(sid, f"contacts_{resident_id}", namespace=settings.DASHBOARD_NAMESPACE)
                        await sio.enter_room(sid, f"contacts:{resident_id}", namespace=settings.DASHBOARD_NAMESPACE)
            finally:
                db.close()
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
        user_id, _role = _resolve_user_id(auth)
        if user_id:
            socket_state.bind(user_id, sid)
            await sio.enter_room(sid, f"resident:{user_id}", namespace=settings.SIGNALING_NAMESPACE)
            await sio.enter_room(sid, f"homeowner:{user_id}", namespace=settings.SIGNALING_NAMESPACE)

    @sio.event(namespace=settings.SIGNALING_NAMESPACE)
    async def disconnect(sid):  # type: ignore[no-redef]
        socket_state.unbind_sid(sid)
        sid_allowed_sessions.pop(sid, None)
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
        visitor_token = (payload or {}).get("visitorToken")
        sender_user_id = socket_state.sid_user.get(sid)

        db = SessionLocal()
        try:
            session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
            if not session:
                await sio.emit(
                    "session.join_denied",
                    {"sessionId": session_id, "reason": "session_not_found"},
                    to=sid,
                    namespace=settings.SIGNALING_NAMESPACE,
                )
                return

            if sender_user_id:
                user = db.query(User).filter(User.id == sender_user_id).first()
                if not user or not _is_user_allowed_for_session(db, user=user, session=session):
                    await sio.emit(
                        "session.join_denied",
                        {"sessionId": session_id, "reason": "not_authorized"},
                        to=sid,
                        namespace=settings.SIGNALING_NAMESPACE,
                    )
                    return
            else:
                try:
                    require_visitor_session_access(db, session=session, visitor_token=visitor_token)
                except Exception:
                    await sio.emit(
                        "session.join_denied",
                        {"sessionId": session_id, "reason": "invalid_visitor_token"},
                        to=sid,
                        namespace=settings.SIGNALING_NAMESPACE,
                    )
                    return
        finally:
            db.close()

        room = f"session:{session_id}"
        await sio.enter_room(sid, room, namespace=settings.SIGNALING_NAMESPACE)
        session_members[room].add(sid)
        sid_allowed_sessions[sid].add(str(session_id))
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
        if str(session_id) not in sid_allowed_sessions.get(sid, set()):
            await sio.emit(
                "chat.ack",
                {
                    "id": "",
                    "sessionId": session_id,
                    "clientId": client_id,
                    "at": datetime.utcnow().isoformat(),
                    "status": "denied",
                },
                to=sid,
                namespace=settings.SIGNALING_NAMESPACE,
            )
            return
        if len(body) > 2000:
            body = body[:2000]
        sender_user_id = socket_state.sid_user.get(sid)
        visitor_token = (payload or {}).get("visitorToken")
        raw_sender_type = (payload or {}).get("senderType")
        optimistic_sender_type = raw_sender_type if raw_sender_type in {"homeowner", "visitor", "security"} else "visitor"
        display_name = (payload or {}).get("displayName") or "Participant"
        created_at = datetime.utcnow().isoformat()
        message_id = str(uuid.uuid4())

        # Validate visitor token again for unauthenticated senders to avoid replay after disconnects.
        if not sender_user_id:
            db = SessionLocal()
            try:
                session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
                if not session:
                    return
                require_visitor_session_access(db, session=session, visitor_token=visitor_token)
            finally:
                db.close()

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

    @sio.on("call.invite", namespace=settings.SIGNALING_NAMESPACE)
    async def call_invite(sid, payload):
        session_id = (payload or {}).get("sessionId")
        if not session_id:
            return
        await sio.emit(
            "call.invite",
            {**(payload or {}), "senderSid": sid, "at": datetime.utcnow().isoformat()},
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )

    @sio.on("call.accepted", namespace=settings.SIGNALING_NAMESPACE)
    async def call_accepted(sid, payload):
        session_id = (payload or {}).get("sessionId")
        if not session_id:
            return
        await sio.emit(
            "call.accepted",
            {**(payload or {}), "senderSid": sid, "at": datetime.utcnow().isoformat()},
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )

    @sio.on("call.rejected", namespace=settings.SIGNALING_NAMESPACE)
    async def call_rejected(sid, payload):
        session_id = (payload or {}).get("sessionId")
        if not session_id:
            return
        await sio.emit(
            "call.rejected",
            {**(payload or {}), "senderSid": sid, "at": datetime.utcnow().isoformat()},
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )

    @sio.on("call.ended", namespace=settings.SIGNALING_NAMESPACE)
    async def call_ended(sid, payload):
        session_id = (payload or {}).get("sessionId")
        if not session_id:
            return
        await sio.emit(
            "call.ended",
            {**(payload or {}), "senderSid": sid, "at": datetime.utcnow().isoformat()},
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )

    @sio.on("visitor-arrived", namespace=settings.SIGNALING_NAMESPACE)
    async def visitor_arrived(sid, payload):
        homeowner_id = (payload or {}).get("homeownerId")
        if not homeowner_id:
            return
        raw = payload or {}
        event_payload = {
            "sessionId": str(raw.get("sessionId") or "").strip(),
            "callSessionId": str(raw.get("callSessionId") or "").strip(),
            "visitorId": str(raw.get("visitorId") or raw.get("sessionId") or "").strip(),
            "appointmentId": str(raw.get("appointmentId") or "").strip() or None,
            "homeownerId": homeowner_id,
            "visitorName": raw.get("visitorName"),
            "doorId": raw.get("doorId"),
            "hasVideo": bool(raw.get("hasVideo", False)),
            "state": raw.get("state") or "ringing",
            "message": raw.get("message"),
            "senderSid": sid,
            "at": datetime.utcnow().isoformat(),
        }
        await sio.emit(
            "incoming-call",
            event_payload,
            room=f"user:{homeowner_id}",
            namespace=settings.DASHBOARD_NAMESPACE,
        )
        await sio.emit(
            "incoming-call",
            event_payload,
            room=f"homeowner:{homeowner_id}",
            namespace=settings.SIGNALING_NAMESPACE,
        )
