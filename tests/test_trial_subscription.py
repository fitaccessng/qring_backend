from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Subscription, User, UserRole
from app.services.payment_service import ensure_signup_trial_subscription, get_effective_subscription


class SignupTrialSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_new_homeowner_signup_creates_trial_subscription_from_created_at(self):
        created_at = datetime.now() - timedelta(days=5)
        user = User(
            id=str(uuid.uuid4()),
            full_name="Ada Lovelace",
            email="ada@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
            created_at=created_at,
        )
        self.db.add(user)
        self.db.commit()

        subscription = ensure_signup_trial_subscription(self.db, user.id, now=created_at + timedelta(days=5))
        self.db.refresh(user)

        self.assertEqual(subscription.payment_status, "trialing")
        self.assertEqual(subscription.plan, "free")
        self.assertEqual(subscription.status, "active")
        self.assertEqual(subscription.trial_started_at, created_at)
        self.assertEqual(subscription.trial_ends_at, created_at + timedelta(days=30))
        self.assertEqual(subscription.starts_at, created_at)
        self.assertEqual(subscription.ends_at, created_at + timedelta(days=30))

        self.assertEqual(len(self.db.query(Subscription).filter(Subscription.user_id == user.id).all()), 1)

        effective = get_effective_subscription(self.db, user.id, user_role=user.role.value)
        self.assertTrue(effective.get("inSignupTrial"))
        self.assertEqual(effective.get("trialDaysRemaining"), 25)

        duplicate = ensure_signup_trial_subscription(self.db, user.id, now=created_at + timedelta(days=6))
        self.assertEqual(duplicate.id, subscription.id)


if __name__ == "__main__":
    unittest.main()
