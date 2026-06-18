from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime

from app.core.config import get_settings
from app.core.security import decode_token
from app.db.models import CallSession, Estate, ResidentSetting, Message, Notification, User, UserRole, VisitorSession
from app.db.session import SessionLocal
from app.socket.contracts import RealtimeEvent
from app.socket.manager import socket_state
from app.services.call_service import (
    end_call_session,
    mark_call_session_answered,
    mark_call_session_connected,
    mark_call_session_connecting,
    mark_call_session_rejected,
)
from app.services.visitor_session_auth import require_visitor_session_access

settings = get_settings()
CHAT_PERSIST_RETRY_DELAYS = (0.35, 1.0, 2.0)
logger = logging.getLogger(__name__)


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


def _socket_log(event: str, **fields) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, "", [], {}))
    logger.info("socket.%s %s", event, details)


def register_socket_events(sio):
    async def _event_context(db, *, sid: str, session_id: str | None, payload: dict | None) -> tuple[str | None, str]:
        user_id = await socket_state.get_user_id(sid)
        role = "visitor"
        normalized_session_id = str(session_id or "").strip()
        if user_id and normalized_session_id:
            session = db.query(VisitorSession).filter(VisitorSession.id == normalized_session_id).first()
            if session and user_id == session.homeowner_id:
                role = "homeowner"
            else:
                user = db.query(User).filter(User.id == user_id).first()
                if user and user.role == UserRole.security:
                    role = "security"
        elif payload and str((payload or {}).get("senderType") or "").strip():
            role = str((payload or {}).get("senderType") or "visitor").strip()
        return user_id, role

    def _event_envelope(
        *,
        event_id: str,
        session_id: str | None,
        user_id: str | None,
        role: str,
        payload: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        return {
            **(payload or {}),
            "eventId": str(event_id or "").strip(),
            "sessionId": str(session_id or (payload or {}).get("sessionId") or "").strip() or None,
            "userId": str(user_id or "").strip() or None,
            "role": str(role or "").strip() or "visitor",
            "timestamp": datetime.utcnow().isoformat(),
            "idempotencyKey": str(
                idempotency_key
                or (payload or {}).get("idempotencyKey")
                or (payload or {}).get("clientId")
                or event_id
                or ""
            ).strip()
            or None,
        }

    async def _participant_type_for_sid(db, sid: str, sender_user_id: str | None, session: VisitorSession) -> str:
        if sender_user_id and sender_user_id == session.homeowner_id:
            return "homeowner"
        if sender_user_id:
            user = db.query(User).filter(User.id == sender_user_id).first()
            if user and user.role == UserRole.security:
                return "security"
        return "visitor"

    async def _active_call_snapshot(db, session_id: str) -> dict | None:
        row = (
            db.query(CallSession)
            .filter(CallSession.visitor_session_id == str(session_id))
            .filter(CallSession.status.in_(["ringing", "accepted", "connecting", "connected", "reconnecting"]))
            .order_by(CallSession.created_at.desc())
            .first()
        )
        if not row:
            return None
        return {
            "callSessionId": row.id,
            "status": row.status,
            "type": row.call_type,
            "hasVideo": row.call_type == "video",
            "visitorId": row.visitor_id,
            "roomName": row.room_name,
            "appointmentId": row.appointment_id,
            "initiatedByRole": row.initiated_by_role,
            "answeredAt": row.answered_at.isoformat() if row.answered_at else None,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        }

    def _latest_snapshot_meta(db, session_id: str) -> dict:
        row = (
            db.query(Notification)
            .filter(
                Notification.kind.in_(["visitor.request", "access_request"]),
                Notification.payload.isnot(None),
            )
            .order_by(Notification.created_at.desc())
            .all()
        )
        for item in row:
            try:
                payload = json.loads(item.payload or "{}")
            except Exception:
                continue
            if str(payload.get("sessionId") or "").strip() != str(session_id):
                continue
            return {
                "snapshotAuditId": str(payload.get("snapshotAuditId") or "").strip() or None,
                "photoUrl": str(payload.get("snapshotUrl") or payload.get("photoUrl") or "").strip() or None,
            }
        return {"snapshotAuditId": None, "photoUrl": None}

    async def _build_session_snapshot_payload(db, session_id: str, *, joined_at: str | None = None) -> dict:
        session = db.query(VisitorSession).filter(VisitorSession.id == str(session_id)).first()
        participants = await socket_state.session_participants(str(session_id))
        active_call = await _active_call_snapshot(db, str(session_id))
        snapshot_meta = _latest_snapshot_meta(db, str(session_id))
        return {
            "sessionId": str(session_id),
            "status": str(session.status or "") if session else "",
            "participants": participants,
            "activeCall": active_call,
            "joinedAt": joined_at,
            "visitorName": session.visitor_label if session and session.visitor_label else "Visitor",
            "visitorPhone": session.visitor_phone if session else None,
            "purpose": session.purpose if session else None,
            "photoUrl": snapshot_meta["photoUrl"] or (str(session.snapshot_url or session.photo_url or "").strip() if session else None),
            "snapshotUrl": snapshot_meta["photoUrl"] or (str(session.snapshot_url or session.photo_url or "").strip() if session else None),
            "snapshotAuditId": snapshot_meta["snapshotAuditId"],
        }

    async def _emit_session_snapshot(session_id: str, *, to: str | None = None) -> None:
        if not session_id:
            return
        db = SessionLocal()
        try:
            payload = await _build_session_snapshot_payload(db, str(session_id))
        finally:
            db.close()
        emit_kwargs = {"namespace": settings.SIGNALING_NAMESPACE}
        if to:
            emit_kwargs["to"] = to
        else:
            emit_kwargs["room"] = f"session:{session_id}"
        await sio.emit(RealtimeEvent.SESSION_SNAPSHOT, payload, **emit_kwargs)

    async def _emit_session_presence(session_id: str) -> None:
        if not session_id:
            return
        participants = await socket_state.session_participants(str(session_id))
        await sio.emit(
            RealtimeEvent.SESSION_PRESENCE,
            {
                "sessionId": str(session_id),
                "participants": participants,
                "count": len(participants),
                "at": datetime.utcnow().isoformat(),
            },
            room=f"session:{session_id}",
            namespace=settings.SIGNALING_NAMESPACE,
        )

    async def _get_allowed_session_id(sid: str, payload) -> str | None:
        session_id = str((payload or {}).get("sessionId") or "").strip()
        if not session_id:
            return None
        if not await socket_state.is_session_allowed(sid, session_id):
            return None
        return session_id

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
                    RealtimeEvent.CHAT_PERSISTED,
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
            RealtimeEvent.CHAT_PERSIST_FAILED,
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
        _socket_log("dashboard_connect", sid=sid, user_id=user_id)
        await sio.emit(
            "dashboard.snapshot",
            {"data": {"message": "connected"}},
            to=sid,
            namespace=settings.DASHBOARD_NAMESPACE,
        )

    @sio.event(namespace=settings.DASHBOARD_NAMESPACE)
    async def disconnect(sid):
        _socket_log("dashboard_disconnect", sid=sid)

    @sio.on("dashboard.subscribe", namespace=settings.DASHBOARD_NAMESPACE)
    async def dashboard_subscribe(sid, payload):
        room = (payload or {}).get("room")
        if room:
            await sio.enter_room(sid, room, namespace=settings.DASHBOARD_NAMESPACE)

    @sio.event(namespace=settings.SIGNALING_NAMESPACE)
    async def connect(sid, environ, auth):  # type: ignore[no-redef]
        user_id, _role = _resolve_user_id(auth)
        if user_id:
            await socket_state.bind(user_id, sid)
            await sio.enter_room(sid, f"resident:{user_id}", namespace=settings.SIGNALING_NAMESPACE)
            await sio.enter_room(sid, f"homeowner:{user_id}", namespace=settings.SIGNALING_NAMESPACE)
        _socket_log("signaling_connect", sid=sid, user_id=user_id, has_auth=bool(auth))

    @sio.event(namespace=settings.SIGNALING_NAMESPACE)
    async def disconnect(sid):  # type: ignore[no-redef]
        room_counts = await socket_state.unbind_sid(sid)
        _socket_log("signaling_disconnect", sid=sid, room_counts=room_counts)
        for item in room_counts:
            await sio.emit(
                RealtimeEvent.SESSION_PARTICIPANT_LEFT,
                {
                    "sid": sid,
                    "count": item.get("count", 0),
                    "participant": item.get("participant"),
                },
                room=item["room"],
                namespace=settings.SIGNALING_NAMESPACE,
            )
            await _emit_session_presence(str(item.get("sessionId") or ""))

    @sio.on(RealtimeEvent.SESSION_LEAVE, namespace=settings.SIGNALING_NAMESPACE)
    async def session_leave(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        await sio.leave_room(sid, f"session:{session_id}", namespace=settings.SIGNALING_NAMESPACE)
        await socket_state.update_session_participant(
            sid,
            session_id,
            presence="offline",
            callState="idle",
            lastSeenAt=datetime.utcnow().isoformat(),
        )
        await _emit_session_presence(session_id)
        return {"ok": True, "sessionId": session_id}

    @sio.on(RealtimeEvent.SESSION_JOIN, namespace=settings.SIGNALING_NAMESPACE)
    async def session_join(sid, payload):
        session_id = (payload or {}).get("sessionId")
        if not session_id:
            return {"ok": False, "reason": "session_id_required"}
        normalized_session_id = str(session_id)
        already_joined = await socket_state.is_session_allowed(sid, normalized_session_id)
        visitor_token = (payload or {}).get("visitorToken")
        sender_user_id = await socket_state.get_user_id(sid)

        db = SessionLocal()
        try:
            session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
            if not session:
                _socket_log("session_join_denied", sid=sid, session_id=session_id, reason="session_not_found")
                await sio.emit(
                    RealtimeEvent.SESSION_JOIN_DENIED,
                    {"sessionId": session_id, "reason": "session_not_found"},
                    to=sid,
                    namespace=settings.SIGNALING_NAMESPACE,
                )
                await socket_state.record_session_join_denied()
                return {"ok": False, "reason": "session_not_found"}

            if sender_user_id:
                user = db.query(User).filter(User.id == sender_user_id).first()
                if not user or not _is_user_allowed_for_session(db, user=user, session=session):
                    _socket_log("session_join_denied", sid=sid, session_id=session_id, reason="not_authorized", user_id=sender_user_id)
                    await sio.emit(
                        RealtimeEvent.SESSION_JOIN_DENIED,
                        {"sessionId": session_id, "reason": "not_authorized"},
                        to=sid,
                        namespace=settings.SIGNALING_NAMESPACE,
                    )
                    await socket_state.record_session_join_denied()
                    return {"ok": False, "reason": "not_authorized"}
            else:
                try:
                    require_visitor_session_access(db, session=session, visitor_token=visitor_token)
                except Exception:
                    _socket_log("session_join_denied", sid=sid, session_id=session_id, reason="invalid_visitor_token")
                    await sio.emit(
                        RealtimeEvent.SESSION_JOIN_DENIED,
                        {"sessionId": session_id, "reason": "invalid_visitor_token"},
                        to=sid,
                        namespace=settings.SIGNALING_NAMESPACE,
                    )
                    await socket_state.record_session_join_denied()
                    return {"ok": False, "reason": "invalid_visitor_token"}
            participant_type = await _participant_type_for_sid(db, sid, sender_user_id, session)
            session_status = str(session.status or "")
            active_call = await _active_call_snapshot(db, str(session_id))
        finally:
            db.close()

        room = f"session:{session_id}"
        if not already_joined:
            await sio.enter_room(sid, room, namespace=settings.SIGNALING_NAMESPACE)
        now_iso = datetime.utcnow().isoformat()
        participant_count = await socket_state.allow_session(
            sid,
            str(session_id),
            {
                "userId": sender_user_id,
                "participantType": participant_type,
                "displayName": (payload or {}).get("displayName") or "Participant",
                "presence": "online",
                "callState": active_call["status"] if active_call else "idle",
                "joinedAt": now_iso,
                "lastSeenAt": now_iso,
            },
        )
        _socket_log(
            "session_joined",
            sid=sid,
            session_id=session_id,
            participant_count=participant_count,
            display_name=(payload or {}).get("displayName"),
            user_id=sender_user_id,
        )
        if not already_joined:
            await sio.emit(
                RealtimeEvent.SESSION_PARTICIPANT_JOINED,
                {
                    "sid": sid,
                    "displayName": (payload or {}).get("displayName") or "Participant",
                    "participantType": participant_type,
                    "count": participant_count,
                },
                room=room,
                skip_sid=sid,
                namespace=settings.SIGNALING_NAMESPACE,
            )
        await sio.emit(
            RealtimeEvent.SESSION_JOINED,
            {"sid": sid, "count": participant_count, "sessionId": str(session_id)},
            to=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        await _emit_session_presence(str(session_id))
        db = SessionLocal()
        try:
            snapshot_payload = await _build_session_snapshot_payload(db, str(session_id), joined_at=now_iso)
        finally:
            db.close()
        await sio.emit(
            RealtimeEvent.SESSION_SNAPSHOT,
            snapshot_payload,
            to=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )

        return {
            "ok": True,
            "sessionId": str(session_id),
            "count": participant_count,
            "status": session_status,
            "activeCall": active_call,
        }

    @sio.on(RealtimeEvent.WEBRTC_OFFER, namespace=settings.SIGNALING_NAMESPACE)
    async def webrtc_offer(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        call_session_id = str((payload or {}).get("callSessionId") or "").strip()
        _socket_log("webrtc_offer", sid=sid, session_id=session_id, call_session_id=call_session_id)
        if call_session_id:
            db = SessionLocal()
            try:
                if bool((payload or {}).get("iceRestart")):
                    mark_call_session_connecting(db, call_session_id=call_session_id)
                    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
                    if row and row.status not in {"ended", "missed", "rejected", "failed", "cancelled"}:
                        row.status = "reconnecting"
                        db.commit()
                        db.refresh(row)
                else:
                    mark_call_session_connecting(db, call_session_id=call_session_id)
            finally:
                db.close()
        await sio.emit(
            RealtimeEvent.WEBRTC_OFFER,
            {**(payload or {}), "sender": sid},
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        return {"ok": True, "sessionId": session_id}

    @sio.on(RealtimeEvent.WEBRTC_ANSWER, namespace=settings.SIGNALING_NAMESPACE)
    async def webrtc_answer(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        call_session_id = str((payload or {}).get("callSessionId") or "").strip()
        _socket_log("webrtc_answer", sid=sid, session_id=session_id, call_session_id=call_session_id)
        if call_session_id:
            db = SessionLocal()
            try:
                mark_call_session_connected(db, call_session_id=call_session_id)
            finally:
                db.close()
        await sio.emit(
            RealtimeEvent.WEBRTC_ANSWER,
            {**(payload or {}), "sender": sid},
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        return {"ok": True, "sessionId": session_id}

    @sio.on(RealtimeEvent.WEBRTC_ICE, namespace=settings.SIGNALING_NAMESPACE)
    async def webrtc_ice(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        candidate = (payload or {}).get("candidate") or {}
        candidate_string = str(candidate)
        if " typ relay" in candidate_string:
            await socket_state.record_metric("relayCandidates")
        _socket_log(
            "webrtc_ice",
            sid=sid,
            session_id=session_id,
            call_session_id=(payload or {}).get("callSessionId"),
            candidate_type="relay" if " typ relay" in candidate_string else "host_or_srflx",
        )
        await sio.emit(
            RealtimeEvent.WEBRTC_ICE,
            {**(payload or {}), "sender": sid},
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        return {"ok": True, "sessionId": session_id, "candidateType": "relay" if " typ relay" in candidate_string else "host_or_srflx"}

    @sio.on(RealtimeEvent.CHAT_MESSAGE, namespace=settings.SIGNALING_NAMESPACE)
    async def chat_message(sid, payload):
        session_id = (payload or {}).get("sessionId")
        body = str((payload or {}).get("text") or "").strip()
        client_id = (payload or {}).get("clientId")
        if not session_id or not body:
            return {"ok": False, "reason": "invalid_payload"}
        if not await socket_state.is_session_allowed(sid, str(session_id)):
            await sio.emit(
                RealtimeEvent.CHAT_ACK,
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
            return {"ok": False, "reason": "session_not_joined"}
        if len(body) > 2000:
            body = body[:2000]
        await socket_state.record_metric("chatMessages")
        sender_user_id = await socket_state.get_user_id(sid)
        message_dedupe_key = str((payload or {}).get("idempotencyKey") or client_id or "").strip()
        if message_dedupe_key and not await socket_state.claim_event_once(
            f"chat.message:{session_id}:{sender_user_id or sid}",
            message_dedupe_key,
            ttl_seconds=180,
        ):
            _socket_log(
                "chat_message_duplicate_suppressed",
                sid=sid,
                session_id=session_id,
                sender_user_id=sender_user_id,
                client_id=client_id,
                idempotency_key=message_dedupe_key,
            )
            await sio.emit(
                RealtimeEvent.CHAT_ACK,
                {
                    "id": "",
                    "sessionId": session_id,
                    "clientId": client_id,
                    "at": datetime.utcnow().isoformat(),
                    "status": "duplicate",
                },
                to=sid,
                namespace=settings.SIGNALING_NAMESPACE,
            )
            return {"ok": True, "sessionId": session_id, "status": "duplicate"}
        _socket_log(
            "chat_message",
            sid=sid,
            session_id=session_id,
            sender_user_id=sender_user_id,
            sender_type=(payload or {}).get("senderType"),
            client_id=client_id,
        )
        visitor_token = (payload or {}).get("visitorToken")
        raw_sender_type = (payload or {}).get("senderType")
        optimistic_sender_type = raw_sender_type if raw_sender_type in {"homeowner", "visitor", "security"} else "visitor"
        display_name = (payload or {}).get("displayName") or "Participant"
        created_at = datetime.utcnow().isoformat()
        message_id = str(uuid.uuid4())
        snapshot_meta = {"snapshotAuditId": None, "photoUrl": None}

        # Validate visitor token again for unauthenticated senders to avoid replay after disconnects.
        if not sender_user_id:
            db = SessionLocal()
            try:
                session = db.query(VisitorSession).filter(VisitorSession.id == session_id).first()
                if not session:
                    return
                require_visitor_session_access(db, session=session, visitor_token=visitor_token)
                snapshot_meta = _latest_snapshot_meta(db, str(session_id))
            finally:
                db.close()
        else:
            db = SessionLocal()
            try:
                snapshot_meta = _latest_snapshot_meta(db, str(session_id))
            finally:
                db.close()

        event_payload = _event_envelope(
            event_id=message_id,
            session_id=str(session_id),
            user_id=sender_user_id,
            role=optimistic_sender_type,
            payload={
                "id": message_id,
                "sessionId": session_id,
                "roomId": f"session:{session_id}",
                "text": body,
                "clientId": client_id,
                "senderType": optimistic_sender_type,
                "senderSid": sid,
                "displayName": display_name,
                "at": created_at,
                "persisted": False,
                "photoUrl": snapshot_meta.get("photoUrl"),
                "snapshotUrl": snapshot_meta.get("photoUrl"),
                "snapshotAuditId": snapshot_meta.get("snapshotAuditId"),
            },
            idempotency_key=message_dedupe_key or message_id,
        )

        await sio.emit(
            RealtimeEvent.CHAT_MESSAGE,
            event_payload,
            room=f"session:{session_id}",
            namespace=settings.SIGNALING_NAMESPACE,
        )
        await sio.emit(
            RealtimeEvent.CHAT_ACK,
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
        return {"ok": True, "id": message_id, "sessionId": session_id, "status": "queued"}

    @sio.on(RealtimeEvent.CHAT_TYPING, namespace=settings.SIGNALING_NAMESPACE)
    async def chat_typing(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        await socket_state.record_metric("typingEvents")
        _socket_log("chat_typing", sid=sid, session_id=session_id, is_typing=bool((payload or {}).get("isTyping")))
        await sio.emit(
            RealtimeEvent.CHAT_TYPING,
            {
                "sessionId": session_id,
                "senderType": (payload or {}).get("senderType") or "visitor",
                "displayName": (payload or {}).get("displayName") or "Participant",
                "isTyping": bool((payload or {}).get("isTyping")),
                "at": datetime.utcnow().isoformat(),
            },
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        return {"ok": True, "sessionId": session_id}

    @sio.on(RealtimeEvent.CHAT_READ, namespace=settings.SIGNALING_NAMESPACE)
    async def chat_read(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        await sio.emit(
            RealtimeEvent.CHAT_READ,
            {
                "sessionId": session_id,
                "readerType": (payload or {}).get("readerType") or "participant",
                "at": (payload or {}).get("at") or datetime.utcnow().isoformat(),
            },
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        return {"ok": True, "sessionId": session_id}

    @sio.on(RealtimeEvent.SESSION_CONTROL, namespace=settings.SIGNALING_NAMESPACE)
    async def session_control(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        action = (payload or {}).get("action")
        if not session_id or not action:
            return {"ok": False, "reason": "invalid_payload"}
        _socket_log("session_control", sid=sid, session_id=session_id, action=action)
        await sio.emit(
            RealtimeEvent.SESSION_CONTROL,
            {
                "sessionId": session_id,
                "action": action,
                "senderSid": sid,
                "at": datetime.utcnow().isoformat(),
            },
            room=f"session:{session_id}",
            namespace=settings.SIGNALING_NAMESPACE,
        )
        return {"ok": True, "sessionId": session_id}

    @sio.on(RealtimeEvent.CALL_INVITE, namespace=settings.SIGNALING_NAMESPACE)
    async def call_invite(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        call_session_id = str((payload or {}).get("callSessionId") or "").strip()
        dedupe_key = str((payload or {}).get("idempotencyKey") or call_session_id or "").strip()
        if dedupe_key and not await socket_state.claim_event_once(f"call.invite:{session_id}", dedupe_key):
            _socket_log("call_invite_duplicate_suppressed", sid=sid, session_id=session_id, call_session_id=call_session_id)
            return {"ok": True, "sessionId": session_id, "status": "duplicate"}
        _socket_log("call_invite", sid=sid, session_id=session_id, call_session_id=call_session_id)
        db = SessionLocal()
        try:
            user_id, role = await _event_context(db, sid=sid, session_id=session_id, payload=payload or {})
        finally:
            db.close()
        await socket_state.update_session_participant(sid, session_id, callState="ringing", presence="ringing", lastSeenAt=datetime.utcnow().isoformat())
        await sio.emit(
            RealtimeEvent.CALL_INVITE,
            _event_envelope(
                event_id=call_session_id,
                session_id=session_id,
                user_id=user_id,
                role=role,
                payload={**(payload or {}), "senderSid": sid, "at": datetime.utcnow().isoformat()},
                idempotency_key=dedupe_key or call_session_id,
            ),
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        await _emit_session_presence(session_id)
        await _emit_session_snapshot(session_id)
        return {"ok": True, "sessionId": session_id}

    @sio.on(RealtimeEvent.CALL_INVITE_RECEIVED, namespace=settings.SIGNALING_NAMESPACE)
    async def call_invite_received(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        await socket_state.update_session_participant(
            sid,
            session_id,
            callState="ringing",
            presence="ringing",
            lastSeenAt=datetime.utcnow().isoformat(),
        )
        await _emit_session_presence(session_id)
        return {"ok": True, "sessionId": session_id}

    @sio.on(RealtimeEvent.CALL_ACCEPTED, namespace=settings.SIGNALING_NAMESPACE)
    async def call_accepted(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        call_session_id = str((payload or {}).get("callSessionId") or "").strip()
        dedupe_key = str((payload or {}).get("idempotencyKey") or call_session_id or "").strip()
        if dedupe_key and not await socket_state.claim_event_once(f"call.accepted:{session_id}", dedupe_key):
            _socket_log("call_accepted_duplicate_suppressed", sid=sid, session_id=session_id, call_session_id=call_session_id)
            return {"ok": True, "sessionId": session_id, "callSessionId": call_session_id, "status": "duplicate"}
        _socket_log("call_accepted", sid=sid, session_id=session_id, call_session_id=call_session_id)
        if call_session_id:
            db = SessionLocal()
            try:
                mark_call_session_answered(db, call_session_id=call_session_id)
                user_id, role = await _event_context(db, sid=sid, session_id=session_id, payload=payload or {})
            finally:
                db.close()
        else:
            db = SessionLocal()
            try:
                user_id, role = await _event_context(db, sid=sid, session_id=session_id, payload=payload or {})
            finally:
                db.close()
        await socket_state.update_session_participant(sid, session_id, callState="connecting", presence="in_call", lastSeenAt=datetime.utcnow().isoformat())
        await sio.emit(
            RealtimeEvent.CALL_ACCEPTED,
            _event_envelope(
                event_id=call_session_id,
                session_id=session_id,
                user_id=user_id,
                role=role,
                payload={**(payload or {}), "senderSid": sid, "at": datetime.utcnow().isoformat()},
                idempotency_key=dedupe_key or call_session_id,
            ),
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        await _emit_session_presence(session_id)
        await _emit_session_snapshot(session_id)
        return {"ok": True, "sessionId": session_id, "callSessionId": call_session_id}

    @sio.on(RealtimeEvent.CALL_REJECTED, namespace=settings.SIGNALING_NAMESPACE)
    async def call_rejected(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        call_session_id = str((payload or {}).get("callSessionId") or "").strip()
        dedupe_key = str((payload or {}).get("idempotencyKey") or call_session_id or "").strip()
        if dedupe_key and not await socket_state.claim_event_once(f"call.rejected:{session_id}", dedupe_key):
            _socket_log("call_rejected_duplicate_suppressed", sid=sid, session_id=session_id, call_session_id=call_session_id)
            return {"ok": True, "sessionId": session_id, "callSessionId": call_session_id, "status": "duplicate"}
        _socket_log("call_rejected", sid=sid, session_id=session_id, call_session_id=call_session_id)
        if call_session_id:
            db = SessionLocal()
            try:
                mark_call_session_rejected(db, call_session_id=call_session_id)
                user_id, role = await _event_context(db, sid=sid, session_id=session_id, payload=payload or {})
            finally:
                db.close()
        else:
            db = SessionLocal()
            try:
                user_id, role = await _event_context(db, sid=sid, session_id=session_id, payload=payload or {})
            finally:
                db.close()
        await socket_state.update_session_participant(sid, session_id, callState="idle", presence="online", lastSeenAt=datetime.utcnow().isoformat())
        await sio.emit(
            RealtimeEvent.CALL_REJECTED,
            _event_envelope(
                event_id=call_session_id,
                session_id=session_id,
                user_id=user_id,
                role=role,
                payload={**(payload or {}), "senderSid": sid, "at": datetime.utcnow().isoformat()},
                idempotency_key=dedupe_key or call_session_id,
            ),
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        await _emit_session_presence(session_id)
        await _emit_session_snapshot(session_id)
        return {"ok": True, "sessionId": session_id, "callSessionId": call_session_id}

    @sio.on(RealtimeEvent.CALL_ENDED, namespace=settings.SIGNALING_NAMESPACE)
    async def call_ended(sid, payload):
        session_id = await _get_allowed_session_id(sid, payload)
        if not session_id:
            return {"ok": False, "reason": "session_not_joined"}
        call_session_id = str((payload or {}).get("callSessionId") or "").strip()
        dedupe_key = str((payload or {}).get("idempotencyKey") or call_session_id or "").strip()
        if dedupe_key and not await socket_state.claim_event_once(f"call.ended:{session_id}", dedupe_key):
            _socket_log("call_ended_duplicate_suppressed", sid=sid, session_id=session_id, call_session_id=call_session_id)
            return {"ok": True, "sessionId": session_id, "status": "duplicate"}
        _socket_log("call_ended", sid=sid, session_id=session_id, call_session_id=call_session_id)
        db = SessionLocal()
        try:
            row = await end_call_session(db, call_session_id=call_session_id, reason=(payload or {}).get("reason"))
            user_id, role = await _event_context(db, sid=sid, session_id=session_id, payload=payload or {})
        finally:
            db.close()
        await socket_state.update_session_participant(sid, session_id, callState="idle", presence="online", lastSeenAt=datetime.utcnow().isoformat())
        await sio.emit(
            RealtimeEvent.CALL_ENDED,
            _event_envelope(
                event_id=call_session_id,
                session_id=session_id,
                user_id=user_id,
                role=role,
                payload={**(payload or {}), "senderSid": sid, "at": datetime.utcnow().isoformat()},
                idempotency_key=dedupe_key or call_session_id,
            ),
            room=f"session:{session_id}",
            skip_sid=sid,
            namespace=settings.SIGNALING_NAMESPACE,
        )
        await _emit_session_presence(session_id)
        await _emit_session_snapshot(session_id)
        return {"ok": True, "sessionId": session_id, "callSessionId": call_session_id, "status": row.status if call_session_id else "ended"}

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
        _socket_log("visitor_arrived", sid=sid, homeowner_id=homeowner_id, session_id=event_payload["sessionId"])
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
