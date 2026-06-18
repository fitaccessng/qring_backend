from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Appointment, CallSession, DeviceSession, Door, Estate, Home, User, UserRole, VisitorSession
from app.services.payment_service import activate_subscription
from app.services.call_service import (
    mark_call_session_answered,
    mark_call_session_connected,
    mark_call_session_connecting,
    mark_call_session_rejected,
    end_call_session,
    join_call_as_homeowner,
    join_call_as_visitor,
    start_call_session,
)
from app.api.routes.calls import StartCallPayload, start_call


class CallServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.estate = Estate(
            id=str(uuid.uuid4()),
            name="Estate A",
            owner_id=str(uuid.uuid4()),
        )
        self.home = Home(
            id=str(uuid.uuid4()),
            name="Home A",
            homeowner_id=str(uuid.uuid4()),
            estate_id=self.estate.id,
        )
        self.door = Door(
            id=str(uuid.uuid4()),
            name="Door A",
            home_id=self.home.id,
        )
        self.db.add_all([self.estate, self.home, self.door])
        self.db.flush()

        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner A",
            email="homeowner-a@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.db.add(self.homeowner)
        self.db.flush()
        activate_subscription(self.db, user_id=self.homeowner.id, plan="home_pro", billing_cycle="monthly")

        self.security = User(
            id=str(uuid.uuid4()),
            full_name="Gateman A",
            email="gateman-a@example.com",
            password_hash="hashed",
            role=UserRole.security,
            email_verified=True,
            estate_id=self.estate.id,
            gate_id="Main Gate",
            is_active=True,
        )
        self.db.add(self.security)
        self.db.flush()

        self.appointment = Appointment(
            id=str(uuid.uuid4()),
            homeowner_id=self.homeowner.id,
            home_id=self.home.id,
            door_id=self.door.id,
            visitor_name="Visitor A",
            visitor_contact="+12345678",
            purpose="Delivery",
            starts_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            ends_at=datetime.now(timezone.utc) + timedelta(minutes=55),
            status="accepted",
        )
        self.db.add(self.appointment)
        self.db.commit()

    def _create_session(self, *, preferred_target: str | None = None, request_source: str = "visitor_qr") -> VisitorSession:
        session = VisitorSession(
            id=str(uuid.uuid4()),
            qr_id="qr-test-session",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.homeowner.id,
            appointment_id=self.appointment.id,
            visitor_label="Visitor A",
            status="approved",
            request_source=request_source,
            preferred_communication_target=preferred_target,
            gate_id="Main Gate",
            estate_id=self.estate.id,
        )
        self.db.add(session)
        self.db.commit()
        return session

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_call_initiation_creates_session(self):
        with patch("app.services.call_service.create_notification") as notify_mock:
            row = asyncio.run(
                start_call_session(
                    self.db,
                    appointment_id=self.appointment.id,
                    visitor_id="visitor-device-123",
                    visitor_name="Visitor A",
                )
            )
            self.assertIsNotNone(row.id)
            self.assertEqual(row.status, "ringing")
            self.assertEqual(row.appointment_id, self.appointment.id)
            self.assertTrue(row.room_name.startswith("qring-call-"))
            self.assertIn(row.id, row.room_name)
            notify_mock.assert_called_once()

    def test_homeowner_to_security_call_start_uses_security_target(self):
        session = self._create_session(preferred_target="gateman")
        with patch("app.api.routes.calls.emit_dashboard_notification") as dashboard_mock, patch(
            "app.api.routes.calls.emit_signaling_notification"
        ) as signaling_mock:
            response = asyncio.run(
                start_call(
                    StartCallPayload(sessionId=session.id, type="audio", hasVideo=False),
                    db=self.db,
                    user=self.homeowner,
                )
            )

        data = response["data"]
        row = self.db.query(CallSession).filter(CallSession.id == data["callSessionId"]).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.security_user_id, self.security.id)
        self.assertEqual(row.initiated_by_role, "homeowner")
        self.assertEqual(row.receiver_id, self.security.id)
        self.assertTrue(row.room_name.startswith("qring-call-"))
        dashboard_mock.assert_awaited_once()
        dashboard_kwargs = dashboard_mock.await_args.kwargs
        self.assertEqual(dashboard_kwargs["rooms"], [f"user:{self.security.id}"])
        self.assertEqual(dashboard_kwargs["payload"]["userId"], self.security.id)
        self.assertEqual(dashboard_kwargs["payload"]["payload"]["receiverRole"], "security")
        self.assertEqual(dashboard_kwargs["payload"]["payload"]["homeownerName"], self.homeowner.full_name)
        signaling_mock.assert_awaited_once()

    def test_homeowner_to_security_call_reports_clear_error_when_no_active_security_user(self):
        session = self._create_session(preferred_target="gateman")
        self.security.is_active = False
        self.db.commit()

        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                start_call(
                    StartCallPayload(sessionId=session.id, type="audio", hasVideo=False),
                    db=self.db,
                    user=self.homeowner,
                )
            )

        self.assertIn("No security user is available for this estate.", str(ctx.exception))

    def test_duplicate_active_homeowner_to_security_call_is_prevented(self):
        session = self._create_session(preferred_target="gateman")
        first = asyncio.run(
            start_call_session(
                self.db,
                visitor_session_id=session.id,
                homeowner_id=self.homeowner.id,
                caller_id=self.homeowner.id,
                receiver_id=self.security.id,
                security_user_id=self.security.id,
                call_type="audio",
            )
        )
        second = asyncio.run(
            start_call_session(
                self.db,
                visitor_session_id=session.id,
                homeowner_id=self.homeowner.id,
                caller_id=self.homeowner.id,
                receiver_id=self.security.id,
                security_user_id=self.security.id,
                call_type="audio",
            )
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.room_name, second.room_name)
        self.assertEqual(second.security_user_id, self.security.id)

    def test_retry_after_terminal_security_target_creates_fresh_room(self):
        session = self._create_session(preferred_target="gateman")
        first = asyncio.run(
            start_call_session(
                self.db,
                visitor_session_id=session.id,
                homeowner_id=self.homeowner.id,
                caller_id=self.homeowner.id,
                receiver_id=self.security.id,
                security_user_id=self.security.id,
                call_type="video",
            )
        )
        first.status = "rejected"
        first.ended_at = datetime.now(timezone.utc)
        self.db.commit()

        second = asyncio.run(
            start_call_session(
                self.db,
                visitor_session_id=session.id,
                homeowner_id=self.homeowner.id,
                caller_id=self.homeowner.id,
                receiver_id=self.security.id,
                security_user_id=self.security.id,
                call_type="video",
            )
        )
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.room_name, second.room_name)
        self.assertEqual(second.security_user_id, self.security.id)

    def test_room_join_returns_webrtc_config_without_marking_call_active_early(self):
        call = CallSession(
            id=str(uuid.uuid4()),
            appointment_id=self.appointment.id,
            room_name="qring-session-call-test-room",
            visitor_id="visitor-device-123",
            homeowner_id=self.homeowner.id,
            status="ringing",
        )
        self.db.add(call)
        self.db.commit()

        data = join_call_as_homeowner(self.db, call_session_id=call.id, homeowner_id=self.homeowner.id)
        self.assertEqual(data["roomName"], call.room_name)
        self.assertIn("rtcConfig", data)
        refreshed = self.db.query(CallSession).filter(CallSession.id == call.id).first()
        self.assertEqual(refreshed.status, "ringing")

    def test_call_end_disconnects_and_marks_ended(self):
        call = CallSession(
            id=str(uuid.uuid4()),
            appointment_id=self.appointment.id,
            room_name="qring-session-call-end-room",
            visitor_id="visitor-device-123",
            homeowner_id=self.homeowner.id,
            status="active",
        )
        self.db.add(call)
        self.db.commit()

        ended = asyncio.run(end_call_session(self.db, call_session_id=call.id))
        self.assertEqual(ended.status, "ended")
        self.assertIsNotNone(ended.ended_at)

    def test_visitor_join_requires_matching_visitor_id(self):
        call = CallSession(
            id=str(uuid.uuid4()),
            appointment_id=self.appointment.id,
            room_name="qring-session-call-visitor-room",
            visitor_id="visitor-device-abc",
            homeowner_id=self.homeowner.id,
            status="ringing",
        )
        self.db.add(call)
        self.db.commit()

        joined = join_call_as_visitor(self.db, call_session_id=call.id, visitor_id="visitor-device-abc")
        self.assertEqual(joined["roomName"], call.room_name)
        self.assertEqual(joined["status"], "ringing")
        self.assertIn("rtcConfig", joined)

    def test_start_call_after_terminal_session_creates_a_new_room(self):
        first = asyncio.run(
            start_call_session(
                self.db,
                appointment_id=self.appointment.id,
                visitor_id="visitor-device-123",
                visitor_name="Visitor A",
            )
        )
        first.status = "ended"
        first.ended_at = datetime.now(timezone.utc)
        self.db.commit()

        second = asyncio.run(
            start_call_session(
                self.db,
                appointment_id=self.appointment.id,
                visitor_id="visitor-device-123",
                visitor_name="Visitor A",
            )
        )
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.room_name, second.room_name)
        self.assertTrue(second.room_name.startswith("qring-call-"))

    def test_start_call_reuses_existing_active_call(self):
        first = asyncio.run(
            start_call_session(
                self.db,
                appointment_id=self.appointment.id,
                visitor_id="visitor-device-123",
                visitor_name="Visitor A",
            )
        )
        second = asyncio.run(
            start_call_session(
                self.db,
                appointment_id=self.appointment.id,
                visitor_id="visitor-device-123",
                visitor_name="Visitor A",
            )
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.room_name, second.room_name)

    def test_call_lifecycle_transitions_store_statuses(self):
        call = asyncio.run(
            start_call_session(
                self.db,
                appointment_id=self.appointment.id,
                visitor_id="visitor-device-123",
                visitor_name="Visitor A",
            )
        )

        answered = mark_call_session_answered(self.db, call_session_id=call.id)
        self.assertEqual(answered.status, "accepted")

        connecting = mark_call_session_connecting(self.db, call_session_id=call.id)
        self.assertEqual(connecting.status, "connecting")

        connected = mark_call_session_connected(self.db, call_session_id=call.id)
        self.assertEqual(connected.status, "connected")

        rejected = mark_call_session_rejected(self.db, call_session_id=call.id)
        self.assertEqual(rejected.status, "rejected")

    def test_ringing_call_ends_as_missed(self):
        call = asyncio.run(
            start_call_session(
                self.db,
                appointment_id=self.appointment.id,
                visitor_id="visitor-device-123",
                visitor_name="Visitor A",
            )
        )
        ended = asyncio.run(end_call_session(self.db, call_session_id=call.id))
        self.assertEqual(ended.status, "missed")


if __name__ == "__main__":
    unittest.main()
