import hashlib
import hmac
import json
import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Estate, Home, User, UserRole
from app.services.estate_alert_service import (
    apply_alert_payment_webhook,
    create_estate_alert,
)
from app.services.payment_service import handle_paystack_webhook


class EstateAlertsServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.estate_owner = User(
            id=str(uuid.uuid4()),
            full_name="Estate Owner",
            email="owner@example.com",
            password_hash="hashed",
            role=UserRole.estate,
            email_verified=True,
        )
        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner",
            email="homeowner@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.estate = Estate(id=str(uuid.uuid4()), name="Estate X", owner_id=self.estate_owner.id)
        self.home = Home(id=str(uuid.uuid4()), name="Unit A", estate_id=self.estate.id, homeowner_id=self.homeowner.id)
        self.db.add_all([self.estate_owner, self.homeowner, self.estate, self.home])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_alert_creation_requires_amount_for_payment_request(self):
        with self.assertRaises(Exception):
            create_estate_alert(
                db=self.db,
                estate_id=self.estate.id,
                estate_admin_id=self.estate_owner.id,
                title="Service charge",
                description="March levy",
                alert_type="payment_request",
                amount_due=None,
                due_date=datetime.utcnow(),
            )

    def test_alert_creation_emits_realtime_event(self):
        with patch("app.services.estate_alert_service.sio.start_background_task") as emit_mock:
            created = create_estate_alert(
                db=self.db,
                estate_id=self.estate.id,
                estate_admin_id=self.estate_owner.id,
                title="General notice",
                description="Gate maintenance",
                alert_type="notice",
                amount_due=None,
                due_date=None,
            )
            self.assertEqual(created["title"], "General notice")
            emit_mock.assert_called()

    def test_receipt_generation_on_successful_webhook_payload(self):
        created = create_estate_alert(
            db=self.db,
            estate_id=self.estate.id,
            estate_admin_id=self.estate_owner.id,
            title="Service charge",
            description="March levy",
            alert_type="payment_request",
            amount_due=5000,
            due_date=datetime.utcnow(),
        )

        data = apply_alert_payment_webhook(
            db=self.db,
            metadata={
                "payment_kind": "estate_alert",
                "estate_alert_id": created["id"],
                "homeowner_id": self.homeowner.id,
            },
            reference="qring-alert-ref-1",
            status="success",
            amount_kobo=500000,
            paid_at_iso=datetime.utcnow().isoformat(),
            paystack_transaction_id=1234567,
        )
        self.assertEqual(data["status"], "processed")
        self.assertIn("transactions/1234567", data["receiptUrl"])

    def test_payment_webhook_routes_estate_alert_payments(self):
        created = create_estate_alert(
            db=self.db,
            estate_id=self.estate.id,
            estate_admin_id=self.estate_owner.id,
            title="Estate levy",
            description="Monthly payment",
            alert_type="payment_request",
            amount_due=2500,
            due_date=datetime.utcnow(),
        )
        body = {
            "event": "charge.success",
            "data": {
                "status": "success",
                "amount": 250000,
                "reference": "qring-alert-ref-2",
                "id": 9988,
                "metadata": {
                    "payment_kind": "estate_alert",
                    "estate_alert_id": created["id"],
                    "homeowner_id": self.homeowner.id,
                },
            },
        }
        raw_body = json.dumps(body).encode("utf-8")
        with patch("app.services.payment_service.settings.PAYSTACK_SECRET_KEY", "sk_test_sample"):
            signature = hmac.new(b"sk_test_sample", raw_body, hashlib.sha512).hexdigest()
            result = handle_paystack_webhook(self.db, raw_body=raw_body, signature=signature)
            self.assertEqual(result["status"], "processed")
            self.assertEqual(result["homeownerId"], self.homeowner.id)


if __name__ == "__main__":
    unittest.main()
