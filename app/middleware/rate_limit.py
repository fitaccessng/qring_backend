from __future__ import annotations

import math
import threading
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.redis import get_async_redis_client, prefixed_key
from app.middleware.request_context import get_client_ip


settings = get_settings()

_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local threshold = now - window

redis.call('ZREMRANGEBYSCORE', key, 0, threshold)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry_after = 1
  if oldest[2] ~= nil then
    retry_after = math.ceil(window - (now - tonumber(oldest[2])))
    if retry_after < 1 then
      retry_after = 1
    end
  end
  return {0, count, retry_after}
end

redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window))
count = redis.call('ZCARD', key)
return {1, count, 0}
"""


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        window_seconds: int = 60,
        max_requests: int = 120,
        auth_window_seconds: int = 60,
        auth_max_requests: int = 20,
    ):
        super().__init__(app)
        self.window_seconds = max(1, int(window_seconds))
        self.max_requests = max(1, int(max_requests))
        self.auth_window_seconds = max(1, int(auth_window_seconds))
        self.auth_max_requests = max(1, int(auth_max_requests))
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}
        self._redis = get_async_redis_client()
        self._redis_script = None

    def _limit_for_path(self, path: str) -> tuple[int, int]:
        if path.startswith("/api/v1/auth"):
            return self.auth_window_seconds, self.auth_max_requests
        return self.window_seconds, self.max_requests

    def _get_bucket_key(self, request) -> str:
        ip = getattr(request.state, "client_ip", "") or get_client_ip(request)
        path = request.url.path or ""
        return f"{ip}:{path}"

    def _prune(self, hits: deque[float], now: float, window_seconds: int) -> None:
        threshold = now - window_seconds
        while hits and hits[0] <= threshold:
            hits.popleft()

    async def _check_redis_limit(self, key: str, *, window_seconds: int, max_requests: int) -> tuple[bool, int, int]:
        if self._redis is None:
            return self._check_local_limit(key, window_seconds=window_seconds, max_requests=max_requests)
        if self._redis_script is None:
            self._redis_script = self._redis.register_script(_SLIDING_WINDOW_LUA)

        now = time.time()
        member = f"{now:.6f}:{time.monotonic_ns()}"
        bucket_key = prefixed_key("ratelimit", key)
        allowed, count, retry_after = await self._redis_script(
            keys=[bucket_key],
            args=[str(now), str(window_seconds), str(max_requests), member],
        )
        remaining = max(0, max_requests - int(count))
        return bool(int(allowed)), remaining, int(retry_after)

    def _check_local_limit(self, key: str, *, window_seconds: int, max_requests: int) -> tuple[bool, int, int]:
        now = time.monotonic()
        with self._lock:
            hits = self._buckets.setdefault(key, deque())
            self._prune(hits, now, window_seconds)
            if len(hits) >= max_requests:
                retry_after = max(1, int(math.ceil(window_seconds - (now - hits[0]))))
                return False, 0, retry_after
            hits.append(now)
            remaining = max(0, max_requests - len(hits))
            return True, remaining, 0

    async def dispatch(self, request, call_next):
        path = request.url.path or ""
        if request.method == "OPTIONS" or not path.startswith("/api/v1"):
            return await call_next(request)
        if path.startswith("/api/v1/health"):
            return await call_next(request)

        window_seconds, max_requests = self._limit_for_path(path)
        key = self._get_bucket_key(request)
        allowed, remaining, retry_after = await self._check_redis_limit(
            key,
            window_seconds=window_seconds,
            max_requests=max_requests,
        )

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please retry later."},
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Window": str(window_seconds),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"] = str(window_seconds)
        return response
