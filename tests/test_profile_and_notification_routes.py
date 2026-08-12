from __future__ import annotations

import uuid
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token
from app.db.base import Base
from app.db.models import Estate, PushSubscription, User, UserRole
from app.db.session import get_db
from app.main import fastapi_app


class ProfileAndNotificationRouteTests(unittest.TestCase):
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

        self.manager = User(
            id=str(uuid.uuid4()),
            full_name="Old Manager",
            email="manager-profile@example.com",
            password_hash="hashed",
            role=UserRole.estate,
            email_verified=True,
        )
        self.other_user = User(
            id=str(uuid.uuid4()),
            full_name="Other User",
            email="other-profile@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.estate = Estate(id=str(uuid.uuid4()), name="Green Estate", owner_id=self.manager.id)
        self.db.add_all([self.manager, self.other_user, self.estate])
        self.db.commit()

        def override_get_db():
            try:
                yield self.db
            finally:
                pass

        fastapi_app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(fastapi_app, raise_server_exceptions=False)
        self.headers = {"Authorization": f"Bearer {create_access_token(self.manager.id, self.manager.role.value)}"}

    def tearDown(self) -> None:
        fastapi_app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def test_manager_profile_update_persists_and_ignores_protected_fields(self) -> None:
        response = self.client.put(
            "/api/v1/auth/me",
            headers=self.headers,
            json={
                "fullName": "Kelvin Manager",
                "phone": "08030000000",
                "email": "changed@example.com",
                "role": "admin",
                "subscription": "estate_pro",
                "estateId": "forged",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["fullName"], "Kelvin Manager")
        self.assertEqual(data["phone"], "08030000000")
        self.assertEqual(data["email"], "manager-profile@example.com")
        self.assertEqual(data["role"], "estate")
        self.assertEqual(data["managedEstates"][0]["id"], self.estate.id)

        self.db.expire_all()
        saved = self.db.query(User).filter(User.id == self.manager.id).first()
        self.assertEqual(saved.full_name, "Kelvin Manager")
        self.assertEqual(saved.phone, "08030000000")
        self.assertEqual(saved.email, "manager-profile@example.com")
        self.assertEqual(saved.role, UserRole.estate)

        refreshed = self.client.get("/api/v1/auth/me", headers=self.headers)
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.json()["data"]["fullName"], "Kelvin Manager")

    def test_push_registration_status_duplicate_and_disable_are_user_scoped(self) -> None:
        created = self.client.post(
            "/api/v1/notifications/push-subscriptions",
            headers=self.headers,
            json={"provider": "fcm", "endpoint": "fcm:token-1", "token": "token-1", "keys": {"token": "token-1"}},
        )
        self.assertEqual(created.status_code, 200, created.text)
        duplicate = self.client.post(
            "/api/v1/notifications/push-subscriptions",
            headers=self.headers,
            json={"provider": "fcm", "endpoint": "fcm:token-1", "token": "token-1b", "keys": {"token": "token-1b"}},
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(self.db.query(PushSubscription).filter(PushSubscription.user_id == self.manager.id).count(), 1)

        other = PushSubscription(user_id=self.other_user.id, provider="fcm", endpoint="fcm:token-1", token="token-1", keys_json="{}", is_active=True)
        self.db.add(other)
        self.db.commit()

        status = self.client.get("/api/v1/notifications/push-subscriptions/status", headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["data"]["enabled"])

        disabled = self.client.post(
            "/api/v1/notifications/push-subscriptions/disable",
            headers=self.headers,
            json={"provider": "fcm", "endpoint": "fcm:token-1"},
        )
        self.assertEqual(disabled.status_code, 200)
        self.db.expire_all()
        own = self.db.query(PushSubscription).filter(PushSubscription.user_id == self.manager.id).first()
        other = self.db.query(PushSubscription).filter(PushSubscription.user_id == self.other_user.id).first()
        self.assertFalse(own.is_active)
        self.assertTrue(other.is_active)


if __name__ == "__main__":
    unittest.main()
