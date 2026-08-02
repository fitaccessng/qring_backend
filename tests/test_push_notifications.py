from __future__ import annotations

import uuid
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import PushSubscription, User, UserRole
from app.services.provider_integrations import deactivate_push_subscription, upsert_push_subscription


class PushNotificationSubscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.user = User(
            id=str(uuid.uuid4()),
            full_name="Push User",
            email="push-user@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_deactivate_push_subscription_marks_matching_rows_inactive(self) -> None:
        upsert_push_subscription(
            self.db,
            user_id=self.user.id,
            provider="fcm",
            endpoint="fcm:token-1",
            token="token-1",
            keys={"token": "token-1", "platform": "android"},
        )
        upsert_push_subscription(
            self.db,
            user_id=self.user.id,
            provider="fcm",
            endpoint="fcm:token-2",
            token="token-2",
            keys={"token": "token-2", "platform": "ios"},
        )

        count = deactivate_push_subscription(
            self.db,
            user_id=self.user.id,
            provider="fcm",
            endpoint="fcm:token-1",
        )

        self.assertEqual(count, 1)
        rows = self.db.query(PushSubscription).filter(PushSubscription.user_id == self.user.id).all()
        self.assertEqual(len(rows), 2)
        self.assertFalse(next(row for row in rows if row.endpoint == "fcm:token-1").is_active)
        self.assertTrue(next(row for row in rows if row.endpoint == "fcm:token-2").is_active)


if __name__ == "__main__":
    unittest.main()
