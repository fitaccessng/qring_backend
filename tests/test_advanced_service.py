from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Door, Home, User, UserRole, VisitorSession
from app.services.advanced_service import (
    contribute_split_bill,
    create_digital_receipt,
    create_split_bill,
    create_threat_alert,
    generate_weekly_summary,
    get_split_bill,
    list_live_queue,
)


class AdvancedServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner",
            email="homeowner@test.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.participant = User(
            id=str(uuid.uuid4()),
            full_name="Participant",
            email="participant@test.com",
            password_hash="hashed",
            role=UserRole.estate,
            email_verified=True,
        )
        self.db.add_all([self.homeowner, self.participant])
        self.db.flush()

        self.home = Home(
            id=str(uuid.uuid4()),
            name="Unit A",
            homeowner_id=self.homeowner.id,
        )
        self.db.add(self.home)
        self.db.flush()

        self.door = Door(
            id=str(uuid.uuid4()),
            name="Main Gate",
            home_id=self.home.id,
        )
        self.db.add(self.door)
        self.db.flush()
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_live_queue_sorted_newest_first(self):
        first = VisitorSession(
            qr_id="qr-1",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.homeowner.id,
            visitor_label="Alice",
            status="pending",
            started_at=datetime.utcnow() - timedelta(minutes=10),
        )
        second = VisitorSession(
            qr_id="qr-2",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.homeowner.id,
            visitor_label="Bob",
            status="approved",
            started_at=datetime.utcnow() - timedelta(minutes=2),
        )
        self.db.add_all([first, second])
        self.db.commit()

        rows = list_live_queue(self.db, homeowner_id=self.homeowner.id)
        self.assertEqual(rows[0]["visitorName"], "Bob")
        self.assertEqual(rows[1]["visitorName"], "Alice")

    def test_split_bill_contribution_updates_remaining(self):
        bill = create_split_bill(
            self.db,
            owner_user_id=self.homeowner.id,
            title="Diesel Bill",
            description="Generator maintenance",
            total_amount_kobo=100_000,
            due_at=None,
            participants=[{"userId": self.participant.id, "pledgedAmountKobo": 60_000}],
            currency="NGN",
        )
        self.assertEqual(bill["remainingAmountKobo"], 100_000)

        updated = contribute_split_bill(
            self.db,
            bill_id=bill["id"],
            user_id=self.participant.id,
            amount_kobo=25_000,
        )
        self.assertEqual(updated["paidAmountKobo"], 25_000)
        self.assertEqual(updated["remainingAmountKobo"], 75_000)

        fetched = get_split_bill(self.db, bill["id"])
        self.assertEqual(fetched["paidAmountKobo"], 25_000)

    def test_weekly_summary_counts_visitors_payments_and_pending_alerts(self):
        session = VisitorSession(
            qr_id="qr-3",
            home_id=self.home.id,
            door_id=self.door.id,
            homeowner_id=self.homeowner.id,
            visitor_label="Chris",
            status="pending",
            started_at=datetime.utcnow() - timedelta(days=1),
        )
        self.db.add(session)
        self.db.commit()

        create_digital_receipt(
            self.db,
            owner_user_id=self.homeowner.id,
            reference="rcpt-1",
            amount_kobo=50000,
            currency="NGN",
            purpose="subscription",
            payload={"test": True},
        )

        week_start = (datetime.utcnow() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        summary = generate_weekly_summary(self.db, user_id=self.homeowner.id, week_start_iso=week_start.isoformat())
        self.assertGreaterEqual(summary["visitors"], 1)
        self.assertGreaterEqual(summary["paymentsMade"], 1)

    def test_threat_alert_logging_creates_alert_entry(self):
        data = create_threat_alert(
            self.db,
            homeowner_id=self.homeowner.id,
            visitor_session_id=None,
            risk_score=82,
            category="unknown_face",
            message="Unknown face detected at gate",
            snapshot_audit_id=None,
        )
        self.assertEqual(data["riskScore"], 82)
        self.assertEqual(data["category"], "unknown_face")


if __name__ == "__main__":
    unittest.main()
