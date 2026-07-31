from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import auth_service


class FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None


class FakeDB:
    def __init__(self):
        self.added = []

    def query(self, *args, **kwargs):
        return FakeQuery()

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        return None

    def refresh(self, obj):
        return None


class OfficeSignupFlowTests(unittest.TestCase):
    def test_office_signup_is_left_pending_until_email_verification(self):
        db = FakeDB()

        with patch.object(auth_service, "_validate_password_strength", return_value=None), patch.object(
            auth_service, "_resolve_referrer", return_value=None
        ), patch.object(auth_service, "_queue_email_verification", return_value=None):
            result = auth_service.signup(
                db=db,
                full_name="Ada Lovelace",
                email="ada@example.com",
                password="Abc12345",
                role="office",
            )

        self.assertTrue(result["requiresEmailVerification"])
        self.assertEqual(result["email"], "ada@example.com")


if __name__ == "__main__":
    unittest.main()
