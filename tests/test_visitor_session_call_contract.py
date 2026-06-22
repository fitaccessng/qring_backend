from __future__ import annotations

import unittest
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from app.core.security import create_access_token
from app.db.base import Base
from app.db.models import CallSession, Door, Home, Message, User, UserRole, VisitorSession
from app.db.session import get_db
from app.main import fastapi_app
from app.services.visitor_session_auth import issue_visitor_session_token


class VisitorSessionCallContractTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite://",
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Contract Homeowner",
            email="contract-homeowner@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
            is_active=True,
        )
        self.other_homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Other Homeowner",
            email="other-homeowner@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
            is_active=True,
        )
        self.db.add_all([self.homeowner, self.other_homeowner])
        self.db.flush()

        self.home = Home(
            id=str(uuid.uuid4()),
            name="Unit 9A",
            homeowner_id=self.homeowner.id,
        )
        self.door = Door(
            id=str(uuid.uuid4()),
            name="Main Gate",
            gate_label="Front Gate",
            home_id=self.home.id,
        )
        self.db.add_all([self.home, self.door])
        self.db.flush()

        self.visitor_session = VisitorSession(
            id=str(uuid.uuid4()),
            request_id=f"req-{uuid.uuid4()}",
            qr_id=f"qr-{uuid.uuid4()}",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.homeowner.id,
            visitor_label="Visitor Example",
            visitor_phone="+2348000000000",
            purpose="Delivery",
            status="approved",
            photo_url="https://cdn.example.com/snapshot.jpg",
            snapshot_url="https://cdn.example.com/snapshot.jpg",
        )
        self.db.add(self.visitor_session)
        self.db.flush()

        self.message = Message(
            id=str(uuid.uuid4()),
            session_id=self.visitor_session.id,
            sender_type="visitor",
            sender_id=None,
            receiver_id=self.homeowner.id,
            body="Hello from the visitor",
        )
        self.call_session = CallSession(
            id=str(uuid.uuid4()),
            visitor_session_id=self.visitor_session.id,
            room_name=f"qring-call-{uuid.uuid4()}",
            visitor_id=self.visitor_session.id,
            homeowner_id=self.homeowner.id,
            caller_id=self.homeowner.id,
            receiver_id=self.homeowner.id,
            call_type="audio",
            status="ended",
            ended_at=self.visitor_session.started_at,
            visitor_request_id=self.visitor_session.request_id,
        )
        self.db.add_all([self.message, self.call_session])
        self.db.commit()

        self.homeowner_token = create_access_token(self.homeowner.id, self.homeowner.role.value)
        self.visitor_token = issue_visitor_session_token(self.db, session=self.visitor_session)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        fastapi_app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(fastapi_app, raise_server_exceptions=False)

        patchers = [
            patch("app.api.routes.visitor.sio.emit"),
            patch("app.api.routes.calls.sio.emit"),
            patch("app.api.routes.visitor.emit_dashboard_notification"),
            patch("app.api.routes.visitor.emit_signaling_notification"),
            patch("app.api.routes.calls.emit_dashboard_notification"),
            patch("app.api.routes.calls.emit_signaling_notification"),
        ]
        self._patchers = patchers
        self._mocks = [patcher.start() for patcher in patchers]

    def tearDown(self):
        for patcher in reversed(getattr(self, "_patchers", [])):
            patcher.stop()
        fastapi_app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def test_get_visitor_session_contract(self):
        response = self.client.get(
            f"/api/v1/visitor-sessions/{self.visitor_session.id}",
            headers={"X-Visitor-Token": self.visitor_token},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json().get("data") or {}
        self.assertEqual(payload.get("visitorSessionId"), self.visitor_session.id)
        self.assertEqual(payload.get("visitorRequestId"), self.visitor_session.request_id)
        self.assertEqual(payload.get("status"), "approved")
        self.assertEqual(payload.get("snapshotUrl"), "https://cdn.example.com/snapshot.jpg")
        self.assertIsInstance(payload.get("messages"), list)
        self.assertIsInstance(payload.get("activeCall"), (dict, type(None)))
        self.assertIsInstance(payload.get("homeowner"), dict)
        self.assertIsInstance(payload.get("home"), dict)
        self.assertIsInstance(payload.get("door"), dict)

    def test_get_visitor_request_thread_contract(self):
        response = self.client.get(
            f"/api/v1/visitor-requests/{self.visitor_session.request_id}/thread",
            headers={"Authorization": f"Bearer {self.homeowner_token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json().get("data") or {}
        self.assertEqual(payload.get("visitorRequestId"), self.visitor_session.request_id)
        self.assertEqual(payload.get("visitorSessionId"), self.visitor_session.id)
        self.assertEqual(payload.get("snapshotUrl"), "https://cdn.example.com/snapshot.jpg")
        self.assertIsInstance(payload.get("messages"), list)
        self.assertIsInstance(payload.get("latestCall"), (dict, type(None)))
        self.assertIsInstance(payload.get("activeCall"), (dict, type(None)))

    def test_post_call_request_contract(self):
        response = self.client.post(
            "/api/v1/calls/request",
            headers={"Authorization": f"Bearer {self.homeowner_token}"},
            json={
                "visitorRequestId": self.visitor_session.request_id,
                "type": "video",
                "hasVideo": True,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json().get("data") or {}
        self.assertEqual(payload.get("visitorRequestId"), self.visitor_session.request_id)
        self.assertEqual(payload.get("visitorSessionId"), self.visitor_session.id)
        self.assertEqual(payload.get("callType"), "video")
        self.assertTrue(payload.get("callSessionId"))
        self.assertEqual(payload.get("status"), "ringing")

    def test_call_request_rejects_visitor_auth(self):
        response = self.client.post(
            "/api/v1/calls/request",
            headers={"X-Visitor-Token": self.visitor_token},
            json={
                "visitorRequestId": self.visitor_session.request_id,
                "type": "audio",
                "hasVideo": False,
            },
        )

        self.assertEqual(response.status_code, 403, response.text)

    def test_ice_config_contract(self):
        response = self.client.get("/api/v1/calls/ice-config")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json().get("data") or {}
        self.assertIn("iceServers", payload)
        self.assertIsInstance(payload["iceServers"], list)
        self.assertGreater(len(payload["iceServers"]), 0)

    def test_visitor_request_smoke_shows_snapshot_in_homeowner_thread(self):
        with (
            patch("app.api.routes.visitor.resolve_qr") as mock_resolve_qr,
            patch("app.api.routes.visitor.create_snapshot_audit") as mock_create_snapshot_audit,
            patch("app.api.routes.visitor.notify_security_request"),
        ):
            request_id = f"smoke-{uuid.uuid4()}"
            mock_resolve_qr.return_value = {
                "home_id": self.home.id,
                "doors": [self.door.id],
                "mode": "direct",
            }
            mock_create_snapshot_audit.return_value = {
                "id": "snapshot-visitor-smoke",
                "fileUrl": "https://cdn.example.com/smoke-snapshot.jpg",
                "url": "https://cdn.example.com/smoke-snapshot.jpg",
            }

            request_response = self.client.post(
                "/api/v1/visitor/request",
                json={
                    "requestId": request_id,
                    "qrId": self.visitor_session.qr_id,
                    "doorId": self.door.id,
                    "name": "Smoke Visitor",
                    "phoneNumber": "+2348000000001",
                    "purpose": "delivery",
                    "visitorType": "delivery",
                    "snapshotBase64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII=",
                    "snapshotMime": "image/png",
                    "deviceId": "device-smoke-1",
                    "consentAccepted": True,
                    "consentAcceptedAt": "2026-06-22T00:00:00Z",
                    "consentStorage": "session",
                },
            )

            self.assertEqual(request_response.status_code, 200, request_response.text)
            request_payload = request_response.json().get("data") or {}
            self.assertEqual(request_payload.get("snapshotUrl"), "https://cdn.example.com/smoke-snapshot.jpg")
            self.assertTrue(request_payload.get("sessionId"))

            thread_response = self.client.get(
                f"/api/v1/visitor-requests/{request_id}/thread",
                headers={"Authorization": f"Bearer {self.homeowner_token}"},
            )

            self.assertEqual(thread_response.status_code, 200, thread_response.text)
            thread_payload = thread_response.json().get("data") or {}
            self.assertEqual(thread_payload.get("snapshotUrl"), "https://cdn.example.com/smoke-snapshot.jpg")
            self.assertGreaterEqual(len(thread_payload.get("messages") or []), 1)
            first_message = thread_payload["messages"][0]
            self.assertEqual(first_message.get("messageType"), "visitor_snapshot")
            self.assertEqual(first_message.get("snapshotUrl"), "https://cdn.example.com/smoke-snapshot.jpg")
            self.assertEqual(first_message.get("visitorName"), "Smoke Visitor")


if __name__ == "__main__":
    unittest.main()
