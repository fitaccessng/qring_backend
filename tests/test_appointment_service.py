from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Notification, User, UserRole, VisitorSession
from app.db.models.appointment import Appointment  # noqa: F401
from app.db.models.device_session import DeviceSession  # noqa: F401
from app.db.models.estate import Door, Estate, Home  # noqa: F401
from app.services.appointment_service import accept_appointment_share, create_appointment
from app.services.security_service import update_security_session_status


class AppointmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner Example",
            email="homeowner@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
            is_active=True,
        )
        self.estate_owner = User(
            id=str(uuid.uuid4()),
            full_name="Estate Owner",
            email="estate@example.com",
            password_hash="hashed",
            role=UserRole.estate,
            email_verified=True,
            is_active=True,
        )
        self.security = User(
            id=str(uuid.uuid4()),
            full_name="Gate Security",
            email="security@example.com",
            password_hash="hashed",
            role=UserRole.security,
            email_verified=True,
            is_active=True,
        )
        self.estate = Estate(
            id=str(uuid.uuid4()),
            name="Palm Estate",
            owner_id=self.estate_owner.id,
        )
        self.security.estate_id = self.estate.id
        self.home = Home(
            id=str(uuid.uuid4()),
            name="Unit 4B",
            estate_id=self.estate.id,
            homeowner_id=self.homeowner.id,
        )
        self.door = Door(
            id=str(uuid.uuid4()),
            name="Main Gate",
            gate_label="Gate A",
            home_id=self.home.id,
        )
        self.db.add_all([self.homeowner, self.estate_owner, self.security, self.estate, self.home, self.door])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_create_appointment_without_email_returns_share_code(self):
        starts_at = datetime.utcnow() + timedelta(hours=2)
        ends_at = starts_at + timedelta(hours=1)

        with patch("app.services.appointment_service.require_subscription_feature"), patch(
            "app.services.appointment_service.send_email_smtp"
        ) as send_email_mock:
            data = create_appointment(
                self.db,
                homeowner_id=self.homeowner.id,
                door_id=self.door.id,
                visitor_name="Ada Visitor",
                visitor_contact="+2348000000000",
                visitor_email=None,
                purpose="Delivery",
                starts_at_iso=starts_at.isoformat(),
                ends_at_iso=ends_at.isoformat(),
                geofence_lat=None,
                geofence_lng=None,
                geofence_radius_meters=None,
            )

        self.assertEqual(data["inviteDelivery"], "manual_code")
        self.assertTrue(str(data["inviteCode"]).startswith("asl."))
        self.assertIn("/appointment/asl.", data["shareUrl"])
        send_email_mock.assert_not_called()

    def test_accept_appointment_notifies_homeowner_and_estate_security(self):
        starts_at = datetime.utcnow() + timedelta(hours=3)
        ends_at = starts_at + timedelta(hours=2)

        with patch("app.services.appointment_service.require_subscription_feature"), patch(
            "app.services.appointment_service.send_email_smtp"
        ):
            created = create_appointment(
                self.db,
                homeowner_id=self.homeowner.id,
                door_id=self.door.id,
                visitor_name="Ada Visitor",
                visitor_contact="+2348000000000",
                visitor_email="visitor@example.com",
                purpose="Meeting",
                starts_at_iso=starts_at.isoformat(),
                ends_at_iso=ends_at.isoformat(),
                geofence_lat=6.5,
                geofence_lng=3.3,
                geofence_radius_meters=150,
            )

            accepted = accept_appointment_share(
                self.db,
                share_token=created["shareToken"],
                device_id="visitor-device-1",
                visitor_name="Ada Visitor",
            )

        session = self.db.query(VisitorSession).filter(VisitorSession.id == accepted["sessionId"]).one()
        self.assertEqual(session.estate_id, self.estate.id)
        self.assertEqual(session.gate_id, "Gate A")

        notifications = self.db.query(Notification).all()
        by_user = {(row.user_id, row.kind) for row in notifications}
        self.assertIn((self.homeowner.id, "appointment.accepted"), by_user)
        self.assertIn((self.security.id, "appointment.accepted"), by_user)

    def test_homeowner_can_approve_pending_appointment_session(self):
        session = VisitorSession(
            id=str(uuid.uuid4()),
            qr_id=f"appointment:{uuid.uuid4()}",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.homeowner.id,
            estate_id=self.estate.id,
            gate_id="Gate A",
            visitor_label="Pending Visitor",
            status="pending",
            communication_status="none",
            gate_status="waiting",
        )
        self.db.add(session)
        self.db.commit()

        updated = update_security_session_status(
            self.db,
            session_id=session.id,
            actor=self.homeowner,
            action="approve",
        )

        self.assertEqual(updated.status, "approved")


if __name__ == "__main__":
    unittest.main()
