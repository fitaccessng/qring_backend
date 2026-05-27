from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes.visitor import _validate_visitor_consent
from app.core.exceptions import AppException
from app.db.base import Base
from app.db.models import Door, Home, User, UserRole, VisitorSession
from app.schemas.visitor import VisitorRequestCreate
from app.services.homeowner_service import list_homeowner_session_messages


class VisitorSnapshotAndConsentTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner Test",
            email="homeowner-snapshot@test.com",
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

    def _create_session(self, *, snapshot_url: str | None = None) -> VisitorSession:
        row = VisitorSession(
            id=str(uuid.uuid4()),
            qr_id=f"qr-{uuid.uuid4()}",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.homeowner.id,
            visitor_label="Visitor Example",
            status="submitted",
            photo_url=snapshot_url,
            snapshot_url=snapshot_url,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def test_validate_visitor_consent_rejects_missing_consent(self):
        payload = VisitorRequestCreate(qrId="qr-test")
        with self.assertRaises(AppException):
            _validate_visitor_consent(payload)

    def test_list_homeowner_session_messages_prepends_snapshot_message(self):
        session = self._create_session(snapshot_url="https://example.com/snapshot.jpg")

        rows = list_homeowner_session_messages(self.db, homeowner_id=self.homeowner.id, session_id=session.id)

        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["messageType"], "visitor_snapshot")
        self.assertEqual(rows[0]["snapshotUrl"], "https://example.com/snapshot.jpg")
        self.assertEqual(rows[0]["photoUrl"], "https://example.com/snapshot.jpg")


if __name__ == "__main__":
    unittest.main()
