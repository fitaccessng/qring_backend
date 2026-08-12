from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Office, Subscription, User, UserRole
from app.services import auth_service


class OfficeSignupFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_office_signup_is_left_pending_until_email_verification(self):
        with patch.object(auth_service, "_validate_password_strength", return_value=None), patch.object(
            auth_service, "_resolve_referrer", return_value=None
        ), patch.object(auth_service, "_queue_email_verification", return_value=None):
            result = auth_service.signup(
                db=self.db,
                full_name="Ada Lovelace",
                email="ada@example.com",
                password="Abc12345",
                role="office",
            )

        self.assertTrue(result["requiresEmailVerification"])
        self.assertEqual(result["email"], "ada@example.com")
        office_user = self.db.query(User).filter(User.email == "ada@example.com").first()
        self.assertIsNotNone(office_user)
        self.assertEqual(office_user.role, UserRole.office)
        self.assertFalse(office_user.email_verified)
        self.assertEqual(self.db.query(Office).count(), 0)
        subscription = self.db.query(Subscription).filter(Subscription.user_id == office_user.id).first()
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.billing_scope, "office")


if __name__ == "__main__":
    unittest.main()
