from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes.visitor import _validate_visitor_consent
from app.core.exceptions import AppException
from app.db.base import Base
from app.db.models import Door, Home, Notification, User, UserRole, VisitorSession
from app.schemas.visitor import VisitorRequestCreate
from app.services.advanced_service import create_snapshot_audit, load_snapshot_bytes
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

    def test_create_snapshot_audit_falls_back_to_data_url_when_file_storage_fails(self):
        media_bytes = b"test-image-bytes"
        with (
            patch("app.services.advanced_service.upload_snapshot_to_cloudinary", return_value=None),
            patch("app.services.advanced_service._get_storage_bucket", return_value=None),
            patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")),
        ):
            data = create_snapshot_audit(
                self.db,
                homeowner_id=self.homeowner.id,
                media_bytes=media_bytes,
                filename_hint="snapshot.jpg",
                media_type="photo",
                visitor_session_id=None,
                appointment_id=None,
                source="visitor_qr_scan",
            )

        self.assertTrue(str(data.get("fileUrl") or "").startswith("data:image/jpeg;base64,"))
        blob, logical_type, content_type = load_snapshot_bytes(
            self.db,
            snapshot_id=data["id"],
            requester_user_id=self.homeowner.id,
            is_admin=False,
        )
        self.assertEqual(blob, media_bytes)
        self.assertEqual(logical_type, "photo")
        self.assertEqual(content_type, "image/jpeg")

    def test_create_snapshot_audit_returns_download_route_for_firebase_storage(self):
        class _Blob:
            def __init__(self):
                self.cache_control = ""
                self.metadata = {}

            def upload_from_string(self, *_args, **_kwargs):
                return None

        class _Bucket:
            def blob(self, *_args, **_kwargs):
                return _Blob()

        with (
            patch("app.services.advanced_service.upload_snapshot_to_cloudinary", return_value=None),
            patch("app.services.advanced_service._get_storage_bucket", return_value=_Bucket()),
        ):
            data = create_snapshot_audit(
                self.db,
                homeowner_id=self.homeowner.id,
                media_bytes=b"firebase-bytes",
                filename_hint="snapshot.jpg",
                media_type="photo",
                visitor_session_id="session-firebase-1",
                appointment_id=None,
                source="visitor_qr_scan",
            )

        self.assertTrue(str(data.get("fileUrl") or "").startswith("/api/v1/advanced/visitor/snapshots/"))
        self.assertEqual(data.get("fileUrl"), data.get("url"))

        session = self._create_session(snapshot_url=None)
        notification = Notification(
            user_id=self.homeowner.id,
            kind="visitor.request",
            payload=json.dumps(
                {
                    "sessionId": session.id,
                    "snapshotAuditId": data["id"],
                }
            ),
        )
        self.db.add(notification)
        self.db.commit()

        rows = list_homeowner_session_messages(self.db, homeowner_id=self.homeowner.id, session_id=session.id)
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["messageType"], "visitor_snapshot")
        self.assertTrue(str(rows[0]["snapshotUrl"] or "").startswith("/api/v1/advanced/visitor/snapshots/"))


if __name__ == "__main__":
    unittest.main()
