from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Door, Home, Message, Notification, User, UserRole, VisitorSession
from app.services.homeowner_service import list_homeowner_message_threads


class HomeownerMessageThreadsServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner Test",
            email="homeowner-message-threads@test.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.db.add(self.homeowner)
        self.db.flush()

        self.home = Home(
            id=str(uuid.uuid4()),
            name="Unit 4B",
            homeowner_id=self.homeowner.id,
        )
        self.db.add(self.home)
        self.db.flush()

        self.door = Door(
            id=str(uuid.uuid4()),
            name="Main Gate",
            gate_label="North Gate",
            home_id=self.home.id,
        )
        self.db.add(self.door)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _create_session(self, *, label: str = "Visitor Example", photo_url: str | None = None) -> VisitorSession:
        row = VisitorSession(
            id=str(uuid.uuid4()),
            qr_id=f"qr-{uuid.uuid4()}",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.homeowner.id,
            visitor_label=label,
            status="pending",
            photo_url=photo_url,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _add_notification(self, *, kind: str, session_id: str, snapshot_audit_id: str, created_at: datetime):
        self.db.add(
            Notification(
                id=str(uuid.uuid4()),
                user_id=self.homeowner.id,
                kind=kind,
                payload=json.dumps(
                    {
                        "sessionId": session_id,
                        "snapshotAuditId": snapshot_audit_id,
                        "message": "New visitor request",
                    }
                ),
                created_at=created_at,
            )
        )
        self.db.commit()

    def test_message_threads_include_snapshot_audit_id_from_access_request_notification(self):
        session = self._create_session()
        expected_snapshot_id = str(uuid.uuid4())
        self._add_notification(
            kind="access_request",
            session_id=session.id,
            snapshot_audit_id=expected_snapshot_id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        rows = list_homeowner_message_threads(self.db, homeowner_id=self.homeowner.id)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], session.id)
        self.assertEqual(rows[0]["snapshotAuditId"], expected_snapshot_id)

    def test_message_threads_include_snapshot_audit_id_from_visitor_request_notification(self):
        session = self._create_session()
        expected_snapshot_id = str(uuid.uuid4())
        self._add_notification(
            kind="visitor.request",
            session_id=session.id,
            snapshot_audit_id=expected_snapshot_id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        rows = list_homeowner_message_threads(self.db, homeowner_id=self.homeowner.id)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], session.id)
        self.assertEqual(rows[0]["snapshotAuditId"], expected_snapshot_id)

    def test_message_threads_keep_visitor_message_and_unread_metadata(self):
        session = self._create_session(photo_url="/uploads/snapshot.jpg")
        self.db.add(
            Message(
                id=str(uuid.uuid4()),
                session_id=session.id,
                sender_type="visitor",
                body="Please open the gate",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            )
        )
        self.db.commit()

        rows = list_homeowner_message_threads(self.db, homeowner_id=self.homeowner.id)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["last"], "Please open the gate")
        self.assertEqual(rows[0]["lastSenderType"], "visitor")
        self.assertEqual(rows[0]["unread"], 1)
        self.assertEqual(rows[0]["photoUrl"], "/uploads/snapshot.jpg")


if __name__ == "__main__":
    unittest.main()
