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
from app.services.estate_operations_service import (
    assert_visitor_not_blocked,
    clock_guard,
    create_blocklist_entry,
    create_incident,
    create_package,
    create_vehicle,
    get_incident_detail,
    list_guard_attendance,
    list_incidents,
    list_packages,
    list_vehicles,
    record_vehicle_gate_action,
    update_package_status,
)
from app.services.estate_service import list_estate_access_logs


class EstateOperationsServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.owner = User(id=str(uuid.uuid4()), full_name="Estate Owner", email="ops-owner@example.com", password_hash="hashed", role=UserRole.estate, email_verified=True, created_at=utc_now() - timedelta(days=45))
        self.resident = User(id=str(uuid.uuid4()), full_name="Resident", email="ops-resident@example.com", password_hash="hashed", role=UserRole.homeowner, email_verified=True)
        self.security = User(id=str(uuid.uuid4()), full_name="Security", email="ops-security@example.com", password_hash="hashed", role=UserRole.security, email_verified=True, gate_id="main")
        self.estate = Estate(id=str(uuid.uuid4()), name="Ops Estate", owner_id=self.owner.id)
        self.home = Home(id=str(uuid.uuid4()), name="Unit A", estate_id=self.estate.id, homeowner_id=self.resident.id)
        self.door = Door(id=str(uuid.uuid4()), name="Main Door", home_id=self.home.id, gate_label="main")
        self.resident.estate_id = self.estate.id
        self.security.estate_id = self.estate.id
        self.db.add_all([self.owner, self.resident, self.security, self.estate, self.home, self.door])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _set_plan(self, plan: str) -> None:
        self.db.query(Subscription).filter(Subscription.user_id == self.owner.id).delete()
        self.db.add(
            Subscription(
                user_id=self.owner.id,
                plan=plan,
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

    def test_basic_plan_supports_vehicle_registration_search_and_gate_logs(self):
        self._set_plan("estate_basic")

        vehicle = create_vehicle(self.db, actor=self.resident, plate_number="abc 123 xy", vehicle_type="car", make_model="Toyota", color="Blue")
        self.assertEqual(vehicle["plateNumber"], "ABC 123 XY")

        results = list_vehicles(self.db, actor=self.security, query="abc")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["residentId"], self.resident.id)

        entry = record_vehicle_gate_action(self.db, actor=self.security, vehicle_id=vehicle["id"], action="entry")
        exit_result = record_vehicle_gate_action(self.db, actor=self.security, vehicle_id=vehicle["id"], action="exit")

        self.assertEqual(entry["status"], "recorded")
        self.assertEqual(exit_result["action"], "exit")
        actions = [row.action for row in self.db.query(GateLog).filter(GateLog.home_id == self.home.id).order_by(GateLog.created_at.asc()).all()]
        self.assertEqual(actions, ["vehicle_entry", "vehicle_exit"])

        vehicle_history = list_estate_access_logs(self.db, owner_id=self.owner.id, category="vehicles")
        self.assertEqual([row["action"] for row in vehicle_history], ["exit", "entry"])
        self.assertEqual(vehicle_history[0]["vehiclePlate"], "ABC 123 XY")
        self.assertEqual(vehicle_history[0]["homeName"], self.home.name)
        self.assertEqual(vehicle_history[0]["residentName"], self.resident.full_name)
        self.assertEqual(vehicle_history[0]["gateId"], "main")
        self.assertEqual(vehicle_history[0]["guardName"], self.security.full_name)

    def test_starter_plan_rejects_basic_blocklist_operations(self):
        self._set_plan("estate_starter")

        with self.assertRaises(AppException):
            create_blocklist_entry(self.db, actor=self.owner, visitor_name="Blocked Guest", visitor_phone="08030000000", reason="Prior incident")

    def test_basic_blocklist_blocks_gate_registration_matches(self):
        self._set_plan("estate_basic")

        entry = create_blocklist_entry(self.db, actor=self.owner, visitor_name="Blocked Guest", visitor_phone="08030000000", reason="Prior incident")
        self.assertTrue(entry["active"])

        with self.assertRaises(AppException):
            assert_visitor_not_blocked(self.db, estate_id=self.estate.id, visitor_name="Someone Else", visitor_phone="08030000000")
        with self.assertRaises(AppException):
            assert_visitor_not_blocked(self.db, estate_id=self.estate.id, visitor_name="Blocked Guest", visitor_phone=None)

    def test_plus_plan_supports_packages_attendance_and_incidents(self):
        self._set_plan("estate_plus")

        package = create_package(self.db, actor=self.security, home_id=self.home.id, courier="Dispatch", description="Envelope")
        self.assertEqual(package["status"], "arrived")
        self.assertEqual(len(list_packages(self.db, actor=self.owner)), 1)
        self.assertEqual(self.db.query(Notification).filter(Notification.user_id == self.resident.id).count(), 1)

        collected = update_package_status(self.db, actor=self.resident, package_id=package["id"], status="collected")
        self.assertEqual(collected["status"], "collected")

        shift = clock_guard(self.db, actor=self.security, action="in")
        self.assertEqual(shift["status"], "on_duty")
        ended_shift = clock_guard(self.db, actor=self.security, action="out")
        self.assertEqual(ended_shift["status"], "off_duty")
        self.assertEqual(len(list_guard_attendance(self.db, actor=self.owner)), 1)

        incident = create_incident(self.db, actor=self.security, incident_type="disturbance", severity="high", description="Noise at main gate")
        self.assertEqual(incident["status"], "open")
        self.assertEqual(len(list_incidents(self.db, actor=self.owner)), 1)

    def test_incident_attachment_persists_and_detail_is_authorized(self):
        self._set_plan("estate_plus")

        incident = create_incident(
            self.db,
            actor=self.security,
            incident_type="damage",
            severity="medium",
            description="Broken barrier",
            photo_url="/uploads/visitor-media/security-incident/photo.jpg",
        )
        detail = get_incident_detail(self.db, actor=self.owner, incident_id=incident["id"])

        self.assertEqual(detail["photoUrl"], "/uploads/visitor-media/security-incident/photo.jpg")
        self.assertEqual(detail["reportedByName"], self.security.full_name)
        self.assertEqual(detail["gateId"], "main")

        outside_owner = User(id=str(uuid.uuid4()), full_name="Other Estate", email="other-owner@example.com", password_hash="hashed", role=UserRole.estate, email_verified=True, created_at=utc_now() - timedelta(days=45))
        self.db.add(outside_owner)
        self.db.add(
            Subscription(
                user_id=outside_owner.id,
                plan="estate_plus",
                status="active",
                payment_status="paid",
                billing_cycle="monthly",
                tenant_type="estate",
                tenant_id=outside_owner.id,
                billing_scope="estate",
                starts_at=utc_now(),
                ends_at=utc_now() + timedelta(days=30),
            )
        )
        self.db.commit()

        with self.assertRaises(AppException):
            get_incident_detail(self.db, actor=outside_owner, incident_id=incident["id"])


if __name__ == "__main__":
    unittest.main()
    get_incident_detail,
