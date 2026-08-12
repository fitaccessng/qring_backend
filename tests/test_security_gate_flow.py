from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppException
from app.core.time import utc_now
from app.db.base import Base
from app.db.models import Door, Estate, GateLog, Home, Message, Subscription, User, UserRole, VisitorSession
from app.services.advanced_service import create_snapshot_audit, load_session_snapshot_bytes, load_snapshot_bytes, resolve_session_snapshot_public_url
from app.services.security_service import list_security_message_threads, update_security_session_status


class SecurityGateFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.owner = User(
            id=str(uuid.uuid4()),
            full_name="Estate Owner",
            email="gate-owner@example.com",
            password_hash="hashed",
            role=UserRole.estate,
            email_verified=True,
        )
        self.resident = User(
            id=str(uuid.uuid4()),
            full_name="Resident",
            email="gate-resident@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.security = User(
            id=str(uuid.uuid4()),
            full_name="Security",
            email="gate-security@example.com",
            password_hash="hashed",
            role=UserRole.security,
            email_verified=True,
            gate_id="main",
        )
        self.estate = Estate(id=str(uuid.uuid4()), name="Gate Estate", owner_id=self.owner.id)
        self.home = Home(id=str(uuid.uuid4()), name="Unit A", estate_id=self.estate.id, homeowner_id=self.resident.id)
        self.door = Door(id=str(uuid.uuid4()), name="Main Gate", home_id=self.home.id, gate_label="main")
        self.security.estate_id = self.estate.id
        self.db.add_all([self.owner, self.resident, self.security, self.estate, self.home, self.door])
        self.db.add(
            Subscription(
                user_id=self.owner.id,
                plan="estate_starter",
                status="active",
                payment_status="paid",
                billing_cycle="monthly",
                tenant_type="estate",
                tenant_id=self.owner.id,
                billing_scope="estate",
                starts_at=utc_now(),
                ends_at=utc_now() + timedelta(days=30),
            )
        )
        self.session = VisitorSession(
            id=str(uuid.uuid4()),
            qr_id="qr-test",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.resident.id,
            estate_id=self.estate.id,
            gate_id="main",
            visitor_label="Visitor",
            status="approved",
            gate_status="waiting",
        )
        self.db.add(self.session)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_checkin_and_checkout_are_distinct_persisted_gate_actions(self):
        checked_in = update_security_session_status(
            self.db,
            session_id=self.session.id,
            actor=self.security,
            action="confirm_entry",
        )

        self.assertEqual(checked_in.status, "gate_confirmed")
        self.assertEqual(checked_in.gate_status, "allowed_in")
        self.assertIsNone(checked_in.ended_at)

        checked_out = update_security_session_status(
            self.db,
            session_id=self.session.id,
            actor=self.security,
            action="checkout",
        )

        self.assertEqual(checked_out.status, "completed")
        self.assertEqual(checked_out.gate_status, "checked_out")
        self.assertIsNotNone(checked_out.ended_at)

        actions = [
            row.action
            for row in self.db.query(GateLog).filter(GateLog.visitor_session_id == self.session.id).order_by(GateLog.created_at.asc()).all()
        ]
        self.assertIn("security_confirm_entry", actions)
        self.assertIn("security_checkout", actions)

        with self.assertRaises(AppException):
            update_security_session_status(
                self.db,
                session_id=self.session.id,
                actor=self.security,
                action="checkout",
            )

    def test_security_can_load_snapshot_for_session_in_own_estate(self):
        media_bytes = b"security-snapshot-bytes"
        audit = create_snapshot_audit(
            self.db,
            homeowner_id=self.resident.id,
            media_bytes=media_bytes,
            filename_hint="snapshot.jpg",
            media_type="photo",
            visitor_session_id=self.session.id,
            source="security_test",
        )

        self.assertEqual(
            resolve_session_snapshot_public_url(self.db, self.session.id),
            f"/api/v1/advanced/visitor/snapshots/{audit['id']}/file",
        )
        blob, logical_type, content_type = load_snapshot_bytes(
            self.db,
            snapshot_id=audit["id"],
            requester_user_id=self.security.id,
            is_admin=False,
            requester_role=self.security.role.value,
            requester_estate_id=self.security.estate_id,
        )

        self.assertEqual(blob, media_bytes)
        self.assertEqual(logical_type, "photo")
        self.assertEqual(content_type, "image/jpeg")

    def test_security_can_load_latest_snapshot_by_session(self):
        media_bytes = b"latest-security-snapshot"
        create_snapshot_audit(
            self.db,
            homeowner_id=self.resident.id,
            media_bytes=b"older-snapshot",
            filename_hint="old.jpg",
            media_type="photo",
            visitor_session_id=self.session.id,
            source="security_test",
        )
        create_snapshot_audit(
            self.db,
            homeowner_id=self.resident.id,
            media_bytes=media_bytes,
            filename_hint="latest.jpg",
            media_type="photo",
            visitor_session_id=self.session.id,
            source="security_test",
        )

        blob, logical_type, _content_type = load_session_snapshot_bytes(
            self.db,
            visitor_session_id=self.session.id,
            requester_user_id=self.security.id,
            is_admin=False,
            requester_role=self.security.role.value,
            requester_estate_id=self.security.estate_id,
        )

        self.assertEqual(blob, media_bytes)
        self.assertEqual(logical_type, "photo")

    def test_homeowner_reply_moves_security_thread_to_top(self):
        older_session = VisitorSession(
            id=str(uuid.uuid4()),
            qr_id="qr-older",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.resident.id,
            estate_id=self.estate.id,
            gate_id="main",
            visitor_label="Older Visitor",
            status="forwarded_to_homeowner",
            gate_status="waiting",
            started_at=utc_now() - timedelta(days=2),
        )
        self.db.add(older_session)
        self.db.flush()
        self.db.add(
            Message(
                session_id=older_session.id,
                sender_type="homeowner",
                sender_id=self.resident.id,
                receiver_id=self.security.id,
                body="Please deny entry.",
                created_at=utc_now() + timedelta(minutes=1),
            )
        )
        self.db.commit()

        threads = list_security_message_threads(self.db, security_user_id=self.security.id, limit=10)

        self.assertEqual(threads[0]["id"], older_session.id)
        self.assertEqual(threads[0]["last"], "Please deny entry.")


if __name__ == "__main__":
    unittest.main()
