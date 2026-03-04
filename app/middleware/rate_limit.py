import math
import threading
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


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

    def _get_client_ip(self, request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _limit_for_path(self, path: str) -> tuple[int, int]:
        if path.startswith("/api/v1/auth"):
            return self.auth_window_seconds, self.auth_max_requests
        return self.window_seconds, self.max_requests

    def _get_bucket_key(self, request) -> str:
        ip = self._get_client_ip(request)
        path = request.url.path or ""
        return f"{ip}:{path}"

    def _prune(self, hits: deque[float], now: float, window_seconds: int) -> None:
        threshold = now - window_seconds
        while hits and hits[0] <= threshold:
            hits.popleft()

    async def dispatch(self, request, call_next):
        path = request.url.path or ""
        if request.method == "OPTIONS" or not path.startswith("/api/v1"):
            return await call_next(request)
        if path.startswith("/api/v1/health"):
            return await call_next(request)

        window_seconds, max_requests = self._limit_for_path(path)
        key = self._get_bucket_key(request)
        now = time.monotonic()

        with self._lock:
            hits = self._buckets.setdefault(key, deque())
            self._prune(hits, now, window_seconds)
            if len(hits) >= max_requests:
                retry_after = max(1, int(math.ceil(window_seconds - (now - hits[0]))))
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
            hits.append(now)
            remaining = max(0, max_requests - len(hits))

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"] = str(window_seconds)
        return response
