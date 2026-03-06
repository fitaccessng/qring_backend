import unittest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import CallSession
from app.services.livekit_monitor_service import handle_livekit_webhook_event


class LivekitMonitorServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

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
        self.assertEqual(refreshed.status, "active")

    def test_room_finished_marks_call_ended(self):
        payload = {
            "event": "room_finished",
            "room": {"name": self.call.room_name},
        }
        data = handle_livekit_webhook_event(self.db, payload)
        self.assertTrue(data["handled"])
        refreshed = self.db.query(CallSession).filter(CallSession.id == self.call.id).first()
        self.assertEqual(refreshed.status, "ended")
        self.assertIsNotNone(refreshed.ended_at)


if __name__ == "__main__":
    unittest.main()
