from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.redis import get_async_redis_client, prefixed_key


class SocketState:
    def __init__(self):
        self._redis = get_async_redis_client()
        self._lock = asyncio.Lock()
        self._sid_user: dict[str, str] = {}
        self._sid_sessions: dict[str, set[str]] = {}
        self._room_members: dict[str, set[str]] = {}
        self._user_sids: dict[str, set[str]] = {}
        self._session_participants: dict[str, dict[str, dict[str, Any]]] = {}
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
        }

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

    async def bind(self, user_id: str, sid: str) -> None:
        if self._redis is not None:
            pipe = self._redis.pipeline()
            pipe.hset(self._sid_user_key(), sid, user_id)
            pipe.sadd(self._user_sids_key(user_id), sid)
            pipe.expire(self._user_sids_key(user_id), 60 * 60 * 24)
            pipe.hincrby(prefixed_key("socket", "metrics"), "binds", 1)
            await pipe.execute()
            return
        async with self._lock:
            self._sid_user[sid] = user_id
            self._user_sids.setdefault(user_id, set()).add(sid)
            self._metrics["binds"] += 1

    async def get_user_id(self, sid: str) -> str | None:
        if self._redis is not None:
            return await self._redis.hget(self._sid_user_key(), sid)
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
        if self._redis is not None:
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
            return int(result[-1])
        async with self._lock:
            self._sid_sessions.setdefault(sid, set()).add(session_id)
            self._room_members.setdefault(room, set()).add(sid)
            self._session_participants.setdefault(session_id, {})[sid] = normalized_participant
            self._metrics["sessionJoins"] += 1
            return len(self._room_members[room])

    async def is_session_allowed(self, sid: str, session_id: str) -> bool:
        if self._redis is not None:
            return bool(await self._redis.sismember(self._sid_sessions_key(sid), session_id))
        async with self._lock:
            return session_id in self._sid_sessions.get(sid, set())

    async def update_session_participant(self, sid: str, session_id: str, **updates: Any) -> dict[str, Any] | None:
        if self._redis is not None:
            key = self._session_participants_key(session_id)
            raw = await self._redis.hget(key, sid)
            if not raw:
                return None
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"sid": sid, "sessionId": session_id}
            payload.update({key: value for key, value in updates.items() if value is not None})
            await self._redis.hset(key, sid, json.dumps(payload))
            return payload
        async with self._lock:
            participant = self._session_participants.get(session_id, {}).get(sid)
            if not participant:
                return None
            participant.update({key: value for key, value in updates.items() if value is not None})
            return dict(participant)

    async def session_participants(self, session_id: str) -> list[dict[str, Any]]:
        if self._redis is not None:
            raw = await self._redis.hgetall(self._session_participants_key(session_id))
            participants: list[dict[str, Any]] = []
            for value in (raw or {}).values():
                try:
                    participants.append(json.loads(value))
                except Exception:
                    continue
            return participants
        async with self._lock:
            return [dict(item) for item in self._session_participants.get(session_id, {}).values()]

    async def unbind_sid(self, sid: str) -> list[dict[str, Any]]:
        if self._redis is not None:
            sid_sessions_key = self._sid_sessions_key(sid)
            session_ids = await self._redis.smembers(sid_sessions_key)
            room_counts: list[dict[str, Any]] = []
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
                participant_raw = await self._redis.hget(participants_key, sid)
                pipe.srem(room_key, sid)
                pipe.hdel(participants_key, sid)
                pipe.scard(room_key)
                participant = None
                if participant_raw:
                    try:
                        participant = json.loads(participant_raw)
                    except Exception:
                        participant = None
                room_counts.append(
                    {
                        "room": room,
                        "sessionId": session_id,
                        "participant": participant,
                    }
                )
            results = await pipe.execute()
            index = 4 if user_id else 3
            for item in room_counts:
                item["count"] = int(results[index])
                index += 2
            return room_counts

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

    async def record_metric(self, metric: str, increment: int = 1) -> None:
        await self._increment_metric(metric, increment=increment)

    async def record_session_join_denied(self) -> None:
        await self._increment_metric("sessionJoinDenied")

    async def _increment_metric(self, metric: str, *, increment: int = 1) -> None:
        if self._redis is not None:
            await self._redis.hincrby(prefixed_key("socket", "metrics"), metric, increment)
            return
        async with self._lock:
            self._metrics[metric] = int(self._metrics.get(metric, 0)) + int(increment)

    async def diagnostics(self) -> dict[str, Any]:
        if self._redis is not None:
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
            return {
                "activeSockets": int(sid_count or 0),
                "activeRooms": active_rooms,
                "activeSessions": active_sessions,
                "activeCalls": active_calls,
                "metrics": {key: int(value or 0) for key, value in (metrics or {}).items()},
            }
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
            keys = []
            async for key in self._redis.scan_iter(match=prefixed_key("socket", "*")):
                keys.append(key)
            if keys:
                await self._redis.delete(*keys)
            return
        async with self._lock:
            self._sid_user.clear()
            self._sid_sessions.clear()
            self._room_members.clear()
            self._user_sids.clear()
            self._session_participants.clear()
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
            }


socket_state = SocketState()
