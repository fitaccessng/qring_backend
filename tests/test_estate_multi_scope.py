from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppException
from app.core.time import utc_now
from app.db.base import Base
from app.db.models import Door, Estate, GateLog, Home, Notification, Subscription, User, UserRole
from app.services.estate_alert_service import create_estate_alert, list_estate_alerts, record_poll_vote
from app.services.estate_operations_service import (
    clock_guard,
    create_incident,
    create_package,
    create_vehicle,
    get_incident_detail,
    list_incidents,
    list_packages,
    list_vehicles,
    record_vehicle_gate_action,
)
from app.services.estate_service import (
    create_estate_security_user,
    get_estate_resident_detail,
    get_estate_security_user_detail,
    list_estate_security_users,
)


class EstateMultiScopeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.owner = User(id=str(uuid.uuid4()), full_name="Multi Owner", email="multi-owner@example.com", password_hash="hashed", role=UserRole.estate, email_verified=True, created_at=utc_now() - timedelta(days=45))
        self.estate_a = Estate(id=str(uuid.uuid4()), name="Green Estate", owner_id=self.owner.id)
        self.estate_b = Estate(id=str(uuid.uuid4()), name="Sunrise Estate", owner_id=self.owner.id)
        self.resident_a = User(id=str(uuid.uuid4()), full_name="Green Resident", email="green-resident@example.com", password_hash="hashed", role=UserRole.homeowner, email_verified=True, estate_id=self.estate_a.id)
        self.resident_b = User(id=str(uuid.uuid4()), full_name="Sunrise Resident", email="sunrise-resident@example.com", password_hash="hashed", role=UserRole.homeowner, email_verified=True, estate_id=self.estate_b.id)
        self.guard_a = User(id=str(uuid.uuid4()), full_name="Green Guard", email="green-guard@example.com", password_hash="hashed", role=UserRole.security, email_verified=True, estate_id=self.estate_a.id, gate_id="green-main")
        self.guard_b = User(id=str(uuid.uuid4()), full_name="Sunrise Guard", email="sunrise-guard@example.com", password_hash="hashed", role=UserRole.security, email_verified=True, estate_id=self.estate_b.id, gate_id="sunrise-main")
        self.home_a = Home(id=str(uuid.uuid4()), name="A1", estate_id=self.estate_a.id, homeowner_id=self.resident_a.id)
        self.home_b = Home(id=str(uuid.uuid4()), name="B1", estate_id=self.estate_b.id, homeowner_id=self.resident_b.id)
        self.door_a = Door(id=str(uuid.uuid4()), name="A Door", home_id=self.home_a.id)
        self.door_b = Door(id=str(uuid.uuid4()), name="B Door", home_id=self.home_b.id)
        self.db.add_all([self.owner, self.estate_a, self.estate_b, self.resident_a, self.resident_b, self.guard_a, self.guard_b, self.home_a, self.home_b, self.door_a, self.door_b])
        self.db.add(
            Subscription(
                user_id=self.owner.id,
                plan="estate_growth",
                status="active",
                payment_status="paid",
                billing_cycle="monthly",
                tenant_type="estate",
                tenant_id=self.owner.id,
                billing_scope="estate",
                starts_at=utc_now(),
                ends_at=utc_now() + timedelta(days=30),
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_broadcast_and_poll_are_estate_scoped(self):
        broadcast_a = create_estate_alert(
            self.db,
            estate_id=self.estate_a.id,
            estate_admin_id=self.owner.id,
            title="Green water notice",
            description="A only",
            alert_type="notice",
            amount_due=None,
            due_date=None,
        )
        poll_a = create_estate_alert(
            self.db,
            estate_id=self.estate_a.id,
            estate_admin_id=self.owner.id,
            title="Green gate time",
            description="A poll",
            alert_type="poll",
            amount_due=None,
            due_date=utc_now() + timedelta(days=1),
            poll_options=["9 PM", "10 PM"],
        )
        rows_a = list_estate_alerts(self.db, estate_id=self.estate_a.id, actor_id=self.resident_a.id, actor_role=UserRole.homeowner)
        self.assertIn(broadcast_a["id"], {row["id"] for row in rows_a})
        recipients = {
            row.user_id
            for row in self.db.query(Notification).filter(Notification.kind == "estate.alert").all()
        }
        self.assertIn(self.resident_a.id, recipients)
        self.assertNotIn(self.resident_b.id, recipients)

        with self.assertRaises(AppException):
            list_estate_alerts(self.db, estate_id=self.estate_a.id, actor_id=self.resident_b.id, actor_role=UserRole.homeowner)
        with self.assertRaises(AppException):
            record_poll_vote(self.db, alert_id=poll_a["id"], homeowner_id=self.resident_b.id, option_index=0)

    def test_guard_vehicle_package_and_incident_scope_follow_estate(self):
        vehicle_a = create_vehicle(self.db, actor=self.resident_a, plate_number="abc 111 aa")
        create_vehicle(self.db, actor=self.resident_b, plate_number="abc 222 bb")

        self.assertEqual([row["plateNumber"] for row in list_vehicles(self.db, actor=self.guard_a, query="abc")], ["ABC 111 AA"])
        with self.assertRaises(AppException):
            record_vehicle_gate_action(self.db, actor=self.guard_b, vehicle_id=vehicle_a["id"], action="entry")

        package_a = create_package(self.db, actor=self.guard_a, home_id=self.home_a.id, courier="Dispatch", description="Green parcel")
        with self.assertRaises(AppException):
            create_package(self.db, actor=self.guard_a, home_id=self.home_b.id, courier="Dispatch", description="Wrong estate")
        self.assertEqual([row["id"] for row in list_packages(self.db, actor=self.guard_a)], [package_a["id"]])
        self.assertEqual(list_packages(self.db, actor=self.guard_b), [])
        package_recipients = {
            row.user_id
            for row in self.db.query(Notification).filter(Notification.kind == "package.arrived").all()
        }
        self.assertEqual(package_recipients, {self.resident_a.id})

        incident_a = create_incident(self.db, actor=self.guard_a, incident_type="noise", severity="low", description="Green gate")
        self.assertEqual([row["id"] for row in list_incidents(self.db, actor=self.guard_a)], [incident_a["id"]])
        self.assertEqual(list_incidents(self.db, actor=self.guard_b), [])
        with self.assertRaises(AppException):
            get_incident_detail(self.db, actor=self.guard_b, incident_id=incident_a["id"])

    def test_guard_and_resident_detail_reject_cross_estate_context(self):
        created_guard = create_estate_security_user(
            self.db,
            owner_id=self.owner.id,
            estate_id=self.estate_a.id,
            full_name="Fresh Guard",
            email="fresh-guard@example.com",
            password="secret123",
            phone="08030000000",
            gate_id="green-main",
        )
        listed_ids = {row["id"] for row in list_estate_security_users(self.db, owner_id=self.owner.id, estate_id=self.estate_a.id)}
        self.assertIn(created_guard.id, listed_ids)

        clock_guard(self.db, actor=self.guard_a, action="in")
        self.db.add(GateLog(estate_id=self.estate_a.id, home_id=self.home_a.id, gate_id="green-main", actor_user_id=self.guard_a.id, actor_role="security", action="security_confirm_entry", resulting_status="allowed_in"))
        self.db.commit()

        guard_detail = get_estate_security_user_detail(self.db, owner_id=self.owner.id, estate_id=self.estate_a.id, security_user_id=self.guard_a.id)
        self.assertEqual(guard_detail["estateId"], self.estate_a.id)
        self.assertEqual(guard_detail["summary"]["entriesConfirmed"], 1)
        with self.assertRaises(AppException):
            get_estate_security_user_detail(self.db, owner_id=self.owner.id, estate_id=self.estate_b.id, security_user_id=self.guard_a.id)

        resident_detail = get_estate_resident_detail(self.db, owner_id=self.owner.id, estate_id=self.estate_a.id, resident_id=self.resident_a.id)
        self.assertEqual(resident_detail["personal"]["estateId"], self.estate_a.id)
        self.assertEqual(resident_detail["household"]["homes"][0]["name"], "A1")
        with self.assertRaises(AppException):
            get_estate_resident_detail(self.db, owner_id=self.owner.id, estate_id=self.estate_b.id, resident_id=self.resident_a.id)


if __name__ == "__main__":
    unittest.main()
