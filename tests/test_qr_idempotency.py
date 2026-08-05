from __future__ import annotations

import threading
import time
import unittest
import uuid
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import Estate, Door, QRCode, User, UserRole, Home
from app.services.estate_service import create_estate_shared_selector_qr


class QRIdempotencyTests(unittest.TestCase):
    def setUp(self):
        # Use a file-backed sqlite DB to allow multiple connections/threads
        db_path = os.path.join(os.getcwd(), "test_qr_race.db")
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except Exception:
                pass
        self.engine = create_engine("sqlite:///./test_qr_race.db", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.estate_owner = User(
            id=str(uuid.uuid4()),
            full_name="Estate Owner",
            email=f"owner-q-{uuid.uuid4().hex}@example.com",
            password_hash="x",
            role=UserRole.estate,
            email_verified=True,
        )
        self.estate = Estate(id=str(uuid.uuid4()), name="Test Estate", owner_id=self.estate_owner.id)
        # create one home and two doors belonging to that home (so service finds doors)
        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Homeowner",
            email=f"homeowner-{uuid.uuid4().hex}@example.com",
            password_hash="x",
            role="homeowner",
            email_verified=True,
        )
        self.home = Home(id=str(uuid.uuid4()), name="Unit A", estate_id=self.estate.id, homeowner_id=self.homeowner.id)
        door1 = Door(id=str(uuid.uuid4()), name="Main Gate", home_id=self.home.id)
        door2 = Door(id=str(uuid.uuid4()), name="Side Gate", home_id=self.home.id)
        self.db.add_all([self.estate_owner, self.estate, self.homeowner, self.home, door1, door2])
        self.db.commit()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass

    def test_sequential_creation_is_idempotent(self):
        # First creation
        result1 = create_estate_shared_selector_qr(db=self.db, owner_id=self.estate_owner.id, estate_id=self.estate.id)
        self.assertIn("qrId", result1)
        # Second creation should return the same qr (idempotent)
        result2 = create_estate_shared_selector_qr(db=self.db, owner_id=self.estate_owner.id, estate_id=self.estate.id)
        self.assertEqual(result1["qrId"], result2["qrId"])

    def test_concurrent_creation_returns_single_row(self):
        # Create one selector QR directly
        s1 = self.SessionLocal()
        created = create_estate_shared_selector_qr(db=s1, owner_id=self.estate_owner.id, estate_id=self.estate.id)
        self.assertIn("qrId", created)
        s1.close()

        # Attempt to insert a duplicate row directly and expect an IntegrityError on commit
        from sqlalchemy.exc import IntegrityError

        s2 = self.SessionLocal()
        duplicate = QRCode(
            qr_id=f"qr-{uuid.uuid4().hex[:12]}",
            plan="multi",
            home_id=self.home.id,
            doors_csv=self.home.id,
            mode="selector",
            estate_id=self.estate.id,
            active=True,
        )
        s2.add(duplicate)
        with self.assertRaises(IntegrityError):
            s2.commit()
        s2.rollback()
        s2.close()

        # Ensure DB still has exactly one selector QR for this estate
        s3 = self.SessionLocal()
        rows = s3.query(QRCode).filter(QRCode.estate_id == self.estate.id, QRCode.mode == "selector").all()
        s3.close()
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
