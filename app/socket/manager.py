from __future__ import annotations

import asyncio

from app.core.redis import get_async_redis_client, prefixed_key


class SocketState:
    def __init__(self):
        self._redis = get_async_redis_client()
        self._lock = asyncio.Lock()
        self._sid_user: dict[str, str] = {}
        self._sid_sessions: dict[str, set[str]] = {}
        self._room_members: dict[str, set[str]] = {}

    def _sid_user_key(self) -> str:
        return prefixed_key("socket", "sid-user")

    def _sid_sessions_key(self, sid: str) -> str:
        return prefixed_key("socket", "sid-sessions", sid)

    def _room_members_key(self, room: str) -> str:
        return prefixed_key("socket", "room-members", room)

    async def bind(self, user_id: str, sid: str) -> None:
        if self._redis is not None:
            await self._redis.hset(self._sid_user_key(), sid, user_id)
            return
        async with self._lock:
            self._sid_user[sid] = user_id

    async def get_user_id(self, sid: str) -> str | None:
        if self._redis is not None:
            return await self._redis.hget(self._sid_user_key(), sid)
        async with self._lock:
            return self._sid_user.get(sid)

    async def allow_session(self, sid: str, session_id: str) -> int:
        room = f"session:{session_id}"
        if self._redis is not None:
            sid_key = self._sid_sessions_key(sid)
            room_key = self._room_members_key(room)
            pipe = self._redis.pipeline()
            pipe.sadd(sid_key, session_id)
            pipe.expire(sid_key, 60 * 60 * 24)
            pipe.sadd(room_key, sid)
            pipe.expire(room_key, 60 * 60 * 24)
            pipe.scard(room_key)
            result = await pipe.execute()
            return int(result[-1])
        async with self._lock:
            self._sid_sessions.setdefault(sid, set()).add(session_id)
            self._room_members.setdefault(room, set()).add(sid)
            return len(self._room_members[room])

    async def is_session_allowed(self, sid: str, session_id: str) -> bool:
        if self._redis is not None:
            return bool(await self._redis.sismember(self._sid_sessions_key(sid), session_id))
        async with self._lock:
            return session_id in self._sid_sessions.get(sid, set())

    async def unbind_sid(self, sid: str) -> list[tuple[str, int]]:
        if self._redis is not None:
            sid_sessions_key = self._sid_sessions_key(sid)
            session_ids = await self._redis.smembers(sid_sessions_key)
            room_counts: list[tuple[str, int]] = []
            pipe = self._redis.pipeline()
            pipe.hdel(self._sid_user_key(), sid)
            pipe.delete(sid_sessions_key)
            for session_id in session_ids:
                room = f"session:{session_id}"
                room_key = self._room_members_key(room)
                pipe.srem(room_key, sid)
                pipe.scard(room_key)
            results = await pipe.execute()
            index = 3
            for session_id in session_ids:
                room = f"session:{session_id}"
                count = int(results[index])
                room_counts.append((room, count))
                index += 2
            return room_counts

        async with self._lock:
            self._sid_user.pop(sid, None)
            room_counts: list[tuple[str, int]] = []
            session_ids = self._sid_sessions.pop(sid, set())
            for session_id in session_ids:
                room = f"session:{session_id}"
                members = self._room_members.get(room)
                if not members:
                    continue
                members.discard(sid)
                if not members:
                    self._room_members.pop(room, None)
                    room_counts.append((room, 0))
                else:
                    room_counts.append((room, len(members)))
            return room_counts


socket_state = SocketState()
