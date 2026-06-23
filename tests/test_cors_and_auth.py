from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

from app.main import app, fastapi_app


TEST_ORIGIN = "https://useqring.online"


@fastapi_app.get("/api/v1/__tests__/boom", include_in_schema=False)
def _boom_route():
    raise RuntimeError("boom")


def _materialize_path(path: str) -> str:
    return re.sub(r"{[^}]+}", "test-id", path)


class CorsAndAuthRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, raise_server_exceptions=False)

    def assertCorsHeaders(self, response):
        self.assertEqual(response.headers.get("access-control-allow-origin"), TEST_ORIGIN)
        self.assertEqual(response.headers.get("access-control-allow-credentials"), "true")

    def test_all_registered_routes_accept_preflight(self):
        checked = 0
        for route in fastapi_app.routes:
            if not isinstance(route, APIRoute):
                continue
            methods = sorted(method for method in (route.methods or set()) if method not in {"HEAD", "OPTIONS"})
            if not methods:
                continue
            checked += 1
            response = self.client.options(
                _materialize_path(route.path),
                headers={
                    "Origin": TEST_ORIGIN,
                    "Access-Control-Request-Method": methods[0],
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            self.assertIn(
                response.status_code,
                {200},
                msg=f"Preflight failed for {route.path} methods={methods} status={response.status_code}",
            )
            self.assertCorsHeaders(response)
        self.assertGreater(checked, 0)

    def test_unauthorized_response_preserves_cors_headers(self):
        response = self.client.get("/api/v1/homeowner/messages", headers={"Origin": TEST_ORIGIN})
        self.assertEqual(response.status_code, 401)
        self.assertCorsHeaders(response)

    def test_unhandled_500_response_preserves_cors_headers(self):
        response = self.client.get("/api/v1/__tests__/boom", headers={"Origin": TEST_ORIGIN})
        self.assertEqual(response.status_code, 500)
        self.assertCorsHeaders(response)
        payload = response.json()
        self.assertEqual(payload.get("message"), "Internal server error")

    def test_socketio_polling_handshake_preserves_cors_headers(self):
        response = self.client.get(
            "/socket.io/",
            params={"EIO": "4", "transport": "polling"},
            headers={"Origin": TEST_ORIGIN},
        )
        self.assertEqual(response.status_code, 200)
        self.assertCorsHeaders(response)


if __name__ == "__main__":
    unittest.main()
