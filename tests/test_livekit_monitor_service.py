from __future__ import annotations

import unittest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import CallSession, DeviceSession, Door, Estate, Home, User, UserRole
from app.services.livekit_monitor_service import handle_livekit_webhook_event


class LivekitMonitorServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.owner = User(
            id=str(uuid.uuid4()),
            full_name="Owner",
            email="owner@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner",
            email="homeowner@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.db.add_all([self.owner, self.homeowner])
        self.db.flush()

        self.estate = Estate(
            id=str(uuid.uuid4()),
            name="Estate A",
            owner_id=self.owner.id,
        )
        self.db.add(self.estate)
        self.db.flush()

        self.home = Home(
            id=str(uuid.uuid4()),
            name="Home A",
            homeowner_id=self.homeowner.id,
            estate_id=self.estate.id,
        )
        self.db.add(self.home)
        self.db.flush()

        self.door = Door(
            id=str(uuid.uuid4()),
            name="Door A",
            home_id=self.home.id,
        )
        self.db.add(self.door)
        self.db.flush()

        self.call = CallSession(
            id=str(uuid.uuid4()),
            appointment_id=str(uuid.uuid4()),
            room_name="qring-session-call-webhook-room",
            visitor_id="visitor-device-9",
            homeowner_id=str(uuid.uuid4()),
            status="ringing",
        )
        self.db.add(self.call)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_participant_joined_marks_call_active(self):
        payload = {
            "event": "participant_joined",
            "room": {"name": self.call.room_name},
            "participant": {"identity": "homeowner:test"},
        }
        data = handle_livekit_webhook_event(self.db, payload)
        self.assertTrue(data["handled"])
        refreshed = self.db.query(CallSession).filter(CallSession.id == self.call.id).first()
        self.assertEqual(refreshed.status, "ongoing")

    def test_room_finished_marks_call_ended(self):
        payload = {
            "event": "room_finished",
            "room": {"name": self.call.room_name},
        }
        data = handle_livekit_webhook_event(self.db, payload)
        self.assertTrue(data["handled"])
        refreshed = self.db.query(CallSession).filter(CallSession.id == self.call.id).first()
        self.assertEqual(refreshed.status, "missed")
        self.assertIsNotNone(refreshed.ended_at)


if __name__ == "__main__":
    unittest.main()
