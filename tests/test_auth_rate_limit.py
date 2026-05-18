from __future__ import annotations

import asyncio
import unittest

from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.requests import Request

from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_context import get_client_ip
from app.services import auth_service


def _build_request(headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": headers,
        "client": client,
    }
    return Request(scope)


class AuthRateLimitTests(unittest.TestCase):
    def setUp(self):
        auth_service._failed_login_hits.clear()
        auth_service._failed_login_blocked_until.clear()

    def tearDown(self):
        auth_service._failed_login_hits.clear()
        auth_service._failed_login_blocked_until.clear()

    def test_login_failures_are_scoped_only_by_ip(self):
        for _ in range(auth_service._LOGIN_MAX_FAILURES):
            auth_service._record_login_failure("first@example.com", "203.0.113.10")

        with self.assertRaises(Exception):
            auth_service._enforce_login_rate_limit("second@example.com", "203.0.113.10")

        auth_service._enforce_login_rate_limit("first@example.com", "198.51.100.9")

    def test_client_ip_prefers_forwarded_headers(self):
        request = _build_request(
            headers=[(b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")],
            client=("127.0.0.1", 59342),
        )
        self.assertEqual(get_client_ip(request), "198.51.100.7")

    def test_client_ip_falls_back_to_socket_client(self):
        request = _build_request(headers=[], client=("127.0.0.1", 59342))
        self.assertEqual(get_client_ip(request), "127.0.0.1")

    def test_login_rate_limit_falls_back_when_redis_is_unavailable(self):
        class BrokenRedis:
            def get(self, _key):
                raise RedisConnectionError("redis down")

        original = auth_service.get_redis_client
        auth_service.get_redis_client = lambda: BrokenRedis()
        try:
            auth_service._enforce_login_rate_limit("first@example.com", "203.0.113.10")
            for _ in range(auth_service._LOGIN_MAX_FAILURES):
                auth_service._record_login_failure("first@example.com", "203.0.113.10")
            with self.assertRaises(Exception):
                auth_service._enforce_login_rate_limit("second@example.com", "203.0.113.10")
        finally:
            auth_service.get_redis_client = original


class MiddlewareRateLimitTests(unittest.TestCase):
    def test_middleware_falls_back_to_local_limit_when_redis_is_unavailable(self):
        middleware = RateLimitMiddleware(app=lambda scope, receive, send: None, auth_max_requests=1)

        async def broken_script(**_kwargs):
            raise RedisConnectionError("redis down")

        middleware._redis = object()
        middleware._redis_script = broken_script

        allowed, remaining, retry_after = asyncio.run(
            middleware._check_redis_limit("203.0.113.10:/api/v1/auth/login", window_seconds=60, max_requests=1)
        )

        self.assertTrue(allowed)
        self.assertEqual(remaining, 0)
        self.assertEqual(retry_after, 0)


if __name__ == "__main__":
    unittest.main()
