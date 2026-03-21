from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Appointment, CallSession, DeviceSession, Door, Estate, Home, User, UserRole
from app.services.call_service import (
    end_call_session,
    join_call_as_homeowner,
    join_call_as_visitor,
    start_call_session,
)


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

        self.appointment = Appointment(
            id=str(uuid.uuid4()),
            homeowner_id=self.homeowner.id,
            home_id=self.home.id,
            door_id=self.door.id,
            visitor_name="Visitor A",
            visitor_contact="+12345678",
            purpose="Delivery",
            starts_at=datetime.utcnow() - timedelta(minutes=5),
            ends_at=datetime.utcnow() + timedelta(minutes=55),
            status="accepted",
        )
        self.db.add(self.appointment)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_call_initiation_creates_session(self):
        with patch("app.services.call_service.create_livekit_room") as create_room_mock, patch(
            "app.services.call_service.create_notification"
        ) as notify_mock:
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
            self.assertEqual(row.room_name, f"qring-session-{self.appointment.id}")
            create_room_mock.assert_called_once()
            notify_mock.assert_called_once()

    def test_room_join_generates_token_and_activates_call(self):
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

        with patch("app.services.call_service.issue_livekit_token_for_room") as token_mock:
            token_mock.return_value = {
                "token": "jwt-token",
                "roomName": call.room_name,
                "url": "wss://livekit.example.com",
            }
            data = join_call_as_homeowner(self.db, call_session_id=call.id, homeowner_id=self.homeowner.id)
            self.assertEqual(data["token"], "jwt-token")
            self.assertEqual(data["roomName"], call.room_name)
            refreshed = self.db.query(CallSession).filter(CallSession.id == call.id).first()
            self.assertEqual(refreshed.status, "ongoing")

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

        with patch("app.services.call_service.delete_livekit_room") as delete_room_mock:
            ended = asyncio.run(end_call_session(self.db, call_session_id=call.id))
            self.assertEqual(ended.status, "ended")
            self.assertIsNotNone(ended.ended_at)
            delete_room_mock.assert_called_once_with(call.room_name)

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

        with patch("app.services.call_service.issue_livekit_token_for_room") as token_mock:
            token_mock.return_value = {
                "token": "visitor-jwt",
                "roomName": call.room_name,
                "url": "wss://livekit.example.com",
            }
            joined = join_call_as_visitor(self.db, call_session_id=call.id, visitor_id="visitor-device-abc")
            self.assertEqual(joined["token"], "visitor-jwt")
            self.assertEqual(joined["status"], "ongoing")


if __name__ == "__main__":
    unittest.main()
