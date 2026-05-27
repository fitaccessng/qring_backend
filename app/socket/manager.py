from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from time import time

from app.core.redis import get_async_redis_client, prefixed_key
from app.services.realtime_runtime_service import mark_realtime_state

logger = logging.getLogger(__name__)


class SocketState:
    def __init__(self):
        self._redis = get_async_redis_client()
        self._lock = asyncio.Lock()
        self._sid_user: dict[str, str] = {}
        self._sid_sessions: dict[str, set[str]] = {}
        self._room_members: dict[str, set[str]] = {}
        self._user_sids: dict[str, set[str]] = {}
        self._session_participants: dict[str, dict[str, dict[str, Any]]] = {}
        self._idempotency_seen: dict[str, float] = {}
        self._metrics: dict[str, int] = {
            "binds": 0,
            "disconnects": 0,
            "sessionJoins": 0,
            "sessionJoinDenied": 0,
            "chatMessages": 0,
            "typingEvents": 0,
            "inviteReplayHits": 0,
            "relayCandidates": 0,
            "iceFailures": 0,
            "reconnectRecoveries": 0,
            "duplicateEventsSuppressed": 0,
        }

    async def _mark_redis_failure(self, action: str, exc: Exception) -> None:
        mark_realtime_state(redisConnected=False, redisError=str(exc))
        logger.warning("socket.redis action=%s error=%s", action, exc)

    async def _mark_redis_success(self) -> None:
        mark_realtime_state(redisConnected=True, redisError="")

    async def _bind_local(self, user_id: str, sid: str) -> None:
        async with self._lock:
            self._sid_user[sid] = user_id
            self._user_sids.setdefault(user_id, set()).add(sid)
            self._metrics["binds"] += 1

    async def _allow_session_local(self, sid: str, session_id: str, participant: dict[str, Any]) -> int:
        room = f"session:{session_id}"
        async with self._lock:
            if session_id in self._sid_sessions.get(sid, set()):
                self._session_participants.setdefault(session_id, {})[sid] = participant
                return len(self._room_members.get(room, set()))
            self._sid_sessions.setdefault(sid, set()).add(session_id)
            self._room_members.setdefault(room, set()).add(sid)
            self._session_participants.setdefault(session_id, {})[sid] = participant
            self._metrics["sessionJoins"] += 1
            return len(self._room_members[room])

    async def _update_session_participant_local(self, sid: str, session_id: str, **updates: Any) -> dict[str, Any] | None:
        async with self._lock:
            participant = self._session_participants.get(session_id, {}).get(sid)
            if not participant:
                return None
            participant.update({key: value for key, value in updates.items() if value is not None})
            return dict(participant)

    async def _session_participants_local(self, session_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(item) for item in self._session_participants.get(session_id, {}).values()]

    async def _unbind_local(self, sid: str) -> list[dict[str, Any]]:
        async with self._lock:
            self._metrics["disconnects"] += 1
            user_id = self._sid_user.pop(sid, None)
            if user_id:
                user_sids = self._user_sids.get(user_id)
                if user_sids:
                    user_sids.discard(sid)
                    if not user_sids:
                        self._user_sids.pop(user_id, None)
            room_counts: list[dict[str, Any]] = []
            session_ids = self._sid_sessions.pop(sid, set())
            for session_id in session_ids:
                room = f"session:{session_id}"
                members = self._room_members.get(room)
                participant = self._session_participants.get(session_id, {}).pop(sid, None)
                if session_id in self._session_participants and not self._session_participants[session_id]:
                    self._session_participants.pop(session_id, None)
                if not members:
                    continue
                members.discard(sid)
                if not members:
                    self._room_members.pop(room, None)
                    room_counts.append({"room": room, "sessionId": session_id, "count": 0, "participant": participant})
                else:
                    room_counts.append(
                        {"room": room, "sessionId": session_id, "count": len(members), "participant": participant}
                    )
            return room_counts

    async def _increment_metric_local(self, metric: str, increment: int = 1) -> None:
        async with self._lock:
            self._metrics[metric] = int(self._metrics.get(metric, 0)) + int(increment)

    def _sid_user_key(self) -> str:
        return prefixed_key("socket", "sid-user")

    def _sid_sessions_key(self, sid: str) -> str:
        return prefixed_key("socket", "sid-sessions", sid)

    def _room_members_key(self, room: str) -> str:
        return prefixed_key("socket", "room-members", room)

    def _user_sids_key(self, user_id: str) -> str:
        return prefixed_key("socket", "user-sids", user_id)

    def _session_participants_key(self, session_id: str) -> str:
        return prefixed_key("socket", "session-participants", session_id)

    def _idempotency_key(self, scope: str, event_id: str) -> str:
        return prefixed_key("socket", "idempotency", scope, event_id)

    async def claim_event_once(self, scope: str, event_id: str, ttl_seconds: int = 120) -> bool:
        normalized_scope = str(scope or "").strip()
        normalized_event_id = str(event_id or "").strip()
        if not normalized_scope or not normalized_event_id:
            return True

        cache_key = f"{normalized_scope}:{normalized_event_id}"
        now = time()
        async with self._lock:
            expired = [key for key, expires_at in self._idempotency_seen.items() if expires_at <= now]
            for key in expired:
                self._idempotency_seen.pop(key, None)
            if cache_key in self._idempotency_seen:
                self._metrics["duplicateEventsSuppressed"] = int(self._metrics.get("duplicateEventsSuppressed", 0)) + 1
                return False
            self._idempotency_seen[cache_key] = now + max(5, int(ttl_seconds))

        if self._redis is not None:
            try:
                claimed = await self._redis.set(
                    self._idempotency_key(normalized_scope, normalized_event_id),
                    "1",
                    ex=max(5, int(ttl_seconds)),
                    nx=True,
                )
                await self._mark_redis_success()
                if not claimed:
                    await self._increment_metric("duplicateEventsSuppressed")
                    return False
            except Exception as exc:
                await self._mark_redis_failure("claim_event_once", exc)
        return True

    async def bind(self, user_id: str, sid: str) -> None:
        await self._bind_local(user_id, sid)
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.hset(self._sid_user_key(), sid, user_id)
                pipe.sadd(self._user_sids_key(user_id), sid)
                pipe.expire(self._user_sids_key(user_id), 60 * 60 * 24)
                pipe.hincrby(prefixed_key("socket", "metrics"), "binds", 1)
                await pipe.execute()
                await self._mark_redis_success()
            except Exception as exc:
                await self._mark_redis_failure("bind", exc)

    async def get_user_id(self, sid: str) -> str | None:
        if self._redis is not None:
            try:
                value = await self._redis.hget(self._sid_user_key(), sid)
                await self._mark_redis_success()
                if value:
                    return value
            except Exception as exc:
                await self._mark_redis_failure("get_user_id", exc)
        async with self._lock:
            return self._sid_user.get(sid)

    async def allow_session(self, sid: str, session_id: str, participant: dict[str, Any] | None = None) -> int:
        room = f"session:{session_id}"
        normalized_participant = {
            "sid": sid,
            "sessionId": session_id,
            "userId": str((participant or {}).get("userId") or "").strip() or None,
            "participantType": str((participant or {}).get("participantType") or "visitor").strip() or "visitor",
            "displayName": str((participant or {}).get("displayName") or "Participant").strip() or "Participant",
            "presence": str((participant or {}).get("presence") or "online").strip() or "online",
            "callState": str((participant or {}).get("callState") or "idle").strip() or "idle",
            "joinedAt": str((participant or {}).get("joinedAt") or "").strip(),
            "lastSeenAt": str((participant or {}).get("lastSeenAt") or "").strip(),
        }
        local_count = await self._allow_session_local(sid, session_id, normalized_participant)
        if self._redis is not None:
            try:
                sid_key = self._sid_sessions_key(sid)
                room_key = self._room_members_key(room)
                participants_key = self._session_participants_key(session_id)
                pipe = self._redis.pipeline()
                pipe.sadd(sid_key, session_id)
                pipe.expire(sid_key, 60 * 60 * 24)
                pipe.sadd(room_key, sid)
                pipe.expire(room_key, 60 * 60 * 24)
                pipe.hset(participants_key, sid, json.dumps(normalized_participant))
                pipe.expire(participants_key, 60 * 60 * 24)
                pipe.hincrby(prefixed_key("socket", "metrics"), "sessionJoins", 1)
                pipe.scard(room_key)
                result = await pipe.execute()
                await self._mark_redis_success()
                return int(result[-1])
            except Exception as exc:
                await self._mark_redis_failure("allow_session", exc)
        return local_count

    async def is_session_allowed(self, sid: str, session_id: str) -> bool:
        if self._redis is not None:
            try:
                value = bool(await self._redis.sismember(self._sid_sessions_key(sid), session_id))
                await self._mark_redis_success()
                return value
            except Exception as exc:
                await self._mark_redis_failure("is_session_allowed", exc)
        async with self._lock:
            return session_id in self._sid_sessions.get(sid, set())

    async def update_session_participant(self, sid: str, session_id: str, **updates: Any) -> dict[str, Any] | None:
        local_payload = await self._update_session_participant_local(sid, session_id, **updates)
        if self._redis is not None:
            try:
                key = self._session_participants_key(session_id)
                payload = local_payload or {"sid": sid, "sessionId": session_id}
                payload.update({key: value for key, value in updates.items() if value is not None})
                await self._redis.hset(key, sid, json.dumps(payload))
                await self._mark_redis_success()
                return payload
            except Exception as exc:
                await self._mark_redis_failure("update_session_participant", exc)
        return local_payload

    async def session_participants(self, session_id: str) -> list[dict[str, Any]]:
        if self._redis is not None:
            try:
                raw = await self._redis.hgetall(self._session_participants_key(session_id))
                participants: list[dict[str, Any]] = []
                for value in (raw or {}).values():
                    try:
                        participants.append(json.loads(value))
                    except Exception:
                        continue
                await self._mark_redis_success()
                return participants
            except Exception as exc:
                await self._mark_redis_failure("session_participants", exc)
        return await self._session_participants_local(session_id)

    async def unbind_sid(self, sid: str) -> list[dict[str, Any]]:
        local_room_counts = await self._unbind_local(sid)
        if self._redis is not None:
            try:
                sid_sessions_key = self._sid_sessions_key(sid)
                session_ids = await self._redis.smembers(sid_sessions_key)
                user_id = await self._redis.hget(self._sid_user_key(), sid)
                pipe = self._redis.pipeline()
                pipe.hincrby(prefixed_key("socket", "metrics"), "disconnects", 1)
                pipe.hdel(self._sid_user_key(), sid)
                pipe.delete(sid_sessions_key)
                if user_id:
                    pipe.srem(self._user_sids_key(user_id), sid)
                for session_id in session_ids:
                    room = f"session:{session_id}"
                    room_key = self._room_members_key(room)
                    participants_key = self._session_participants_key(session_id)
                    pipe.srem(room_key, sid)
                    pipe.hdel(participants_key, sid)
                await pipe.execute()
                await self._mark_redis_success()
            except Exception as exc:
                await self._mark_redis_failure("unbind_sid", exc)
        return local_room_counts

    async def record_metric(self, metric: str, increment: int = 1) -> None:
        await self._increment_metric(metric, increment=increment)

    async def record_session_join_denied(self) -> None:
        await self._increment_metric("sessionJoinDenied")

    async def _increment_metric(self, metric: str, *, increment: int = 1) -> None:
        await self._increment_metric_local(metric, increment=increment)
        if self._redis is not None:
            try:
                await self._redis.hincrby(prefixed_key("socket", "metrics"), metric, increment)
                await self._mark_redis_success()
            except Exception as exc:
                await self._mark_redis_failure("increment_metric", exc)

    async def diagnostics(self) -> dict[str, Any]:
        if self._redis is not None:
            try:
                sid_count = await self._redis.hlen(self._sid_user_key())
                metrics = await self._redis.hgetall(prefixed_key("socket", "metrics"))
                room_keys = []
                async for key in self._redis.scan_iter(match=prefixed_key("socket", "room-members", "*")):
                    room_keys.append(key)
                active_rooms = len(room_keys)
                session_keys = []
                async for key in self._redis.scan_iter(match=prefixed_key("socket", "session-participants", "*")):
                    session_keys.append(key)
                active_calls = 0
                active_sessions: dict[str, int] = {}
                for key in session_keys:
                    session_id = str(key).rsplit(":", 1)[-1]
                    participants = await self._redis.hlen(key)
                    active_sessions[session_id] = int(participants or 0)
                    raw_participants = await self._redis.hgetall(key)
                    decoded = []
                    for value in (raw_participants or {}).values():
                        try:
                            decoded.append(json.loads(value))
                        except Exception:
                            continue
                    if any(str(item.get("callState") or "") in {"ringing", "in_call", "connecting"} for item in decoded):
                        active_calls += 1
                mark_realtime_state(redisConnected=True, redisError="")
                return {
                    "activeSockets": int(sid_count or 0),
                    "activeRooms": active_rooms,
                    "activeSessions": active_sessions,
                    "activeCalls": active_calls,
                    "metrics": {key: int(value or 0) for key, value in (metrics or {}).items()},
                    "storageMode": "redis",
                    "adapterConnected": True,
                }
            except Exception as exc:
                mark_realtime_state(redisConnected=False, redisError=str(exc))
                return {
                    **(await self._diagnostics_local()),
                    "storageMode": "memory",
                    "adapterConnected": False,
                    "degraded": True,
                    "error": str(exc),
                }
        diagnostics = await self._diagnostics_local()
        diagnostics["storageMode"] = "memory"
        diagnostics["adapterConnected"] = False
        return diagnostics

    async def _diagnostics_local(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "activeSockets": len(self._sid_user),
                "activeRooms": len(self._room_members),
                "activeSessions": {
                    session_id: len(participants)
                    for session_id, participants in self._session_participants.items()
                },
                "activeCalls": sum(
                    1
                    for participants in self._session_participants.values()
                    if any(str(item.get("callState") or "") in {"ringing", "in_call", "connecting"} for item in participants.values())
                ),
                "metrics": dict(self._metrics),
            }

    async def reset_for_tests(self) -> None:
        if self._redis is not None:
            try:
                keys = []
                async for key in self._redis.scan_iter(match=prefixed_key("socket", "*")):
                    keys.append(key)
                if keys:
                    await self._redis.delete(*keys)
                await self._mark_redis_success()
            except Exception as exc:
                await self._mark_redis_failure("reset_for_tests", exc)
        async with self._lock:
            self._sid_user.clear()
            self._sid_sessions.clear()
            self._room_members.clear()
            self._user_sids.clear()
            self._session_participants.clear()
            self._idempotency_seen.clear()
            self._metrics = {
                "binds": 0,
                "disconnects": 0,
                "sessionJoins": 0,
                "sessionJoinDenied": 0,
                "chatMessages": 0,
                "typingEvents": 0,
                "inviteReplayHits": 0,
                "relayCandidates": 0,
                "iceFailures": 0,
                "reconnectRecoveries": 0,
                "duplicateEventsSuppressed": 0,
            }


socket_state = SocketState()
