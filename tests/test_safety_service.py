from __future__ import annotations

import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Estate, Home, User, UserRole
from app.db.models.device_session import DeviceSession  # noqa: F401
from app.db.models.homeowner_setting import HomeownerSetting  # noqa: F401
from app.db.models.safety import EmergencyAlert, EmergencyAlertEvent, VisitorReport, WatchlistEntry  # noqa: F401
from app.db.models.session import Notification, VisitorSession  # noqa: F401
from app.services.safety_service import report_visitor, trigger_emergency_alert, update_emergency_alert_status


class SafetyServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.estate_owner = User(
            id=str(uuid.uuid4()),
            full_name="Estate Owner",
            email="estate-owner@example.com",
            password_hash="hashed",
            role=UserRole.estate,
            email_verified=True,
            estate_id=None,
        )
        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner",
            email="homeowner@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
            phone="08030000000",
        )
        self.guard = User(
            id=str(uuid.uuid4()),
            full_name="Guard",
            email="guard@example.com",
            password_hash="hashed",
            role=UserRole.security,
            email_verified=True,
        )
        self.estate = Estate(id=str(uuid.uuid4()), name="Safe Estate", owner_id=self.estate_owner.id)
        self.home = Home(id=str(uuid.uuid4()), name="Unit B2", estate_id=self.estate.id, homeowner_id=self.homeowner.id)
        self.guard.estate_id = self.estate.id
        self.estate_owner.estate_id = self.estate.id
        self.db.add_all([self.estate_owner, self.homeowner, self.guard, self.estate, self.home])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_trigger_emergency_alert_creates_events_and_notifications(self):
        with patch("app.services.notification_service.send_push_fcm"):
            payload = trigger_emergency_alert(
                self.db,
                user=self.homeowner,
                alert_type="panic",
                trigger_mode="hold",
                silent_trigger=True,
                cancel_window_seconds=8,
                location={"lat": 6.4, "lng": 3.4, "source": "device_gps"},
                offline_queued=False,
            )

        self.assertEqual(payload["alertType"], "panic")
        self.assertEqual(payload["status"], "dispatched")
        self.assertTrue(payload["silentTrigger"])
        self.assertGreaterEqual(len(payload["events"]), 2)
        self.assertEqual(self.db.query(EmergencyAlert).count(), 1)
        self.assertGreaterEqual(self.db.query(EmergencyAlertEvent).count(), 2)
        self.assertGreaterEqual(self.db.query(Notification).count(), 1)

    def test_acknowledge_and_resolve_alert(self):
        with patch("app.services.notification_service.send_push_fcm"):
            created = trigger_emergency_alert(
                self.db,
                user=self.homeowner,
                alert_type="fire",
                trigger_mode="hold",
                silent_trigger=False,
            )
        acknowledged = update_emergency_alert_status(
            self.db,
            alert_id=created["id"],
            actor=self.guard,
            action="acknowledge",
            notes="Guard dispatched",
        )
        resolved = update_emergency_alert_status(
            self.db,
            alert_id=created["id"],
            actor=self.guard,
            action="resolve",
            notes="Incident cleared",
        )

        self.assertEqual(acknowledged["status"], "acknowledged")
        self.assertEqual(resolved["status"], "resolved")

    def test_report_visitor_updates_watchlist(self):
        session = VisitorSession(
            id=str(uuid.uuid4()),
            qr_id="manual",
            home_id=self.home.id,
            door_id="door-1",
            homeowner_id=self.homeowner.id,
            visitor_label="John Doe",
            status="completed",
            estate_id=self.estate.id,
        )
        self.db.add(session)
        self.db.commit()

        with patch("app.services.notification_service.send_push_fcm"):
            result = report_visitor(
                self.db,
                actor=self.homeowner,
                visitor_session_id=session.id,
                reported_name=None,
                reported_phone="08035551234",
                reason="Attempted forced entry",
                notes="Returned late at night twice.",
                severity="high",
            )

        self.assertEqual(result["report"]["reportedName"], "John Doe")
        self.assertEqual(self.db.query(VisitorReport).count(), 1)
        self.assertEqual(self.db.query(WatchlistEntry).count(), 1)
        self.assertIn(result["watchlistEntry"]["riskLevel"], {"high", "critical"})


if __name__ == "__main__":
    unittest.main()
