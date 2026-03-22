from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models.appointment import Appointment  # noqa: F401
from app.db.models.device_session import DeviceSession  # noqa: F401
from app.db.models.estate import Door, Estate, Home  # noqa: F401
from app.db.models import Notification, Subscription, SubscriptionEvent, SubscriptionNotification, User, UserRole
from app.services.subscription_lifecycle_service import run_subscription_lifecycle_jobs


class SubscriptionLifecycleServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.user = User(
            id=str(uuid.uuid4()),
            full_name="Estate Owner",
            email="owner@example.com",
            password_hash="hashed",
            role=UserRole.estate,
            email_verified=True,
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_warning_job_creates_deduped_warning_notification(self):
        now = datetime(2026, 3, 22, 10, 0, 0)
        subscription = Subscription(
            user_id=self.user.id,
            plan="estate_growth",
            status="active",
            payment_status="active",
            billing_cycle="monthly",
            tenant_type="estate",
            tenant_id=self.user.id,
            billing_scope="estate",
            grace_days=5,
            starts_at=now - timedelta(days=23),
            ends_at=now + timedelta(days=7),
            grace_ends_at=now + timedelta(days=12),
            timezone="Africa/Lagos",
        )
        self.db.add(subscription)
        self.db.commit()

        first_run = run_subscription_lifecycle_jobs(self.db, now=now)
        second_run = run_subscription_lifecycle_jobs(self.db, now=now)

        self.assertEqual(first_run["warnings_sent"], 1)
        self.assertEqual(second_run["warnings_sent"], 0)

        notices = self.db.query(SubscriptionNotification).all()
        app_notifications = self.db.query(Notification).all()
        events = self.db.query(SubscriptionEvent).filter(SubscriptionEvent.event_type == "subscription.warning_sent").all()

        self.assertEqual(len(notices), 1)
        self.assertEqual(len(app_notifications), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(notices[0].template_key, "subscription.expiry_warning")
        self.assertEqual(notices[0].delivery_status, "sent")

        payload = json.loads(app_notifications[0].payload)
        self.assertEqual(payload["daysToExpiry"], 7)
        self.assertEqual(payload["templateKey"], "subscription.expiry_warning")

    def test_job_moves_expired_subscription_into_grace_and_notifies_user(self):
        now = datetime(2026, 3, 22, 10, 0, 0)
        subscription = Subscription(
            user_id=self.user.id,
            plan="estate_growth",
            status="active",
            payment_status="pending",
            billing_cycle="monthly",
            tenant_type="estate",
            tenant_id=self.user.id,
            billing_scope="estate",
            grace_days=5,
            starts_at=now - timedelta(days=31),
            ends_at=now - timedelta(hours=2),
            grace_ends_at=now + timedelta(days=3),
            timezone="Africa/Lagos",
        )
        self.db.add(subscription)
        self.db.commit()

        result = run_subscription_lifecycle_jobs(self.db, now=now)
        self.db.refresh(subscription)

        self.assertEqual(result["entered_grace"], 1)
        self.assertEqual(subscription.status, "grace_period")
        self.assertEqual(subscription.payment_status, "expired")

        event = (
            self.db.query(SubscriptionEvent)
            .filter(SubscriptionEvent.subscription_id == subscription.id, SubscriptionEvent.event_type == "subscription.entered_grace")
            .one()
        )
        notice = (
            self.db.query(SubscriptionNotification)
            .filter(SubscriptionNotification.subscription_id == subscription.id, SubscriptionNotification.template_key == "subscription.grace_started")
            .one()
        )
        app_notification = (
            self.db.query(Notification)
            .filter(Notification.user_id == self.user.id, Notification.kind == "subscription.grace_started")
            .one()
        )

        self.assertEqual(event.new_status, "grace_period")
        self.assertEqual(notice.delivery_status, "sent")
        self.assertIn("grace period", json.loads(app_notification.payload)["message"])

    def test_job_suspends_subscription_after_grace_window(self):
        now = datetime(2026, 3, 22, 10, 0, 0)
        subscription = Subscription(
            user_id=self.user.id,
            plan="estate_growth",
            status="grace_period",
            payment_status="pending",
            billing_cycle="monthly",
            tenant_type="estate",
            tenant_id=self.user.id,
            billing_scope="estate",
            grace_days=5,
            starts_at=now - timedelta(days=40),
            ends_at=now - timedelta(days=6),
            grace_ends_at=now - timedelta(minutes=5),
            timezone="Africa/Lagos",
        )
        self.db.add(subscription)
        self.db.commit()

        result = run_subscription_lifecycle_jobs(self.db, now=now)
        self.db.refresh(subscription)

        self.assertEqual(result["suspended"], 1)
        self.assertEqual(subscription.status, "suspended")
        self.assertEqual(subscription.payment_status, "expired")
        self.assertEqual(subscription.suspension_reason, "non_payment")

        event = (
            self.db.query(SubscriptionEvent)
            .filter(SubscriptionEvent.subscription_id == subscription.id, SubscriptionEvent.event_type == "subscription.suspended")
            .one()
        )
        notice = (
            self.db.query(SubscriptionNotification)
            .filter(SubscriptionNotification.subscription_id == subscription.id, SubscriptionNotification.template_key == "subscription.suspended")
            .one()
        )

        self.assertEqual(event.old_status, "grace_period")
        self.assertEqual(event.new_status, "suspended")
        self.assertEqual(notice.delivery_status, "sent")


if __name__ == "__main__":
    unittest.main()
