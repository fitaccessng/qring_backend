from __future__ import annotations

import unittest
import uuid
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppException
from app.core.time import utc_now
from app.db.base import Base
from app.db.models import DigitalAccessPass, Door, Estate, Home, QRCode, Subscription, User, UserRole
from app.services.payment_service import (
    ESTATE_PLAN_FEATURE_LEVELS,
    calculate_plan_billing_amount,
    get_effective_subscription,
    get_plan_or_raise,
    require_subscription_feature,
)
from app.services.access_pass_service import create_homeowner_access_pass


class EstateSubscriptionBillingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()
        self.owner = User(
            id=str(uuid.uuid4()),
            full_name="Estate Owner",
            email="owner@example.com",
            password_hash="hashed",
            role=UserRole.estate,
            email_verified=True,
            created_at=utc_now() - timedelta(days=45),
        )
        self.db.add(self.owner)
        self.db.flush()
        self.estate = Estate(id=str(uuid.uuid4()), name="Test Estate", owner_id=self.owner.id)
        self.db.add(self.estate)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_basic_plan_bills_extra_houses_without_forcing_upgrade(self):
        for idx in range(33):
            resident = User(
                id=str(uuid.uuid4()),
                full_name=f"Resident {idx}",
                email=f"resident{idx}@example.com",
                password_hash="hashed",
                role=UserRole.homeowner,
                email_verified=True,
                estate_id=self.estate.id,
            )
            self.db.add(resident)
            self.db.flush()
            self.db.add(Home(name=f"Unit {idx}", estate_id=self.estate.id, homeowner_id=resident.id))
        self.db.commit()

        plan = get_plan_or_raise(self.db, "estate_basic")
        billing = calculate_plan_billing_amount(self.db, user_id=self.owner.id, plan=plan, billing_cycle="monthly")

        self.assertEqual(billing["includedHouses"], 30)
        self.assertEqual(billing["activeHouseCount"], 33)
        self.assertEqual(billing["extraHouses"], 3)
        self.assertEqual(billing["extraHouseAmount"], 3500)
        self.assertEqual(billing["monthlyTotal"], 35500)
        self.assertEqual(billing["totalAmountKobo"], 3550000)

    def test_active_starter_plan_does_not_pass_plus_feature(self):
        self.db.add(
            Subscription(
                user_id=self.owner.id,
                plan="estate_starter",
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
        with self.assertRaises(AppException):
            require_subscription_feature(self.db, self.owner.id, "visitor_scheduling", user_role="estate")

    def test_estate_plan_feature_matrix_allows_and_rejects_expected_capabilities(self):
        matrix = {
            "estate_starter": {
                "allowed": ["register_residents", "register_security_guards", "manual_visitor_logging", "visitor_pass_basic"],
                "rejected": ["delivery_management", "visitor_scheduling", "export_reports"],
            },
            "estate_basic": {
                "allowed": ["delivery_management", "emergency_sos", "estate_announcements"],
                "rejected": ["visitor_scheduling", "video_verification", "export_reports"],
            },
            "estate_plus": {
                "allowed": ["visitor_scheduling", "video_verification", "incident_reporting"],
                "rejected": ["export_reports", "multi_admin_roles", "targeted_announcements"],
            },
            "estate_growth": {
                "allowed": ["export_reports", "multi_admin_roles", "targeted_announcements"],
                "rejected": ["multi_location_control", "api_access"],
            },
        }

        for plan_id, expectations in matrix.items():
            with self.subTest(plan=plan_id):
                self.db.query(Subscription).filter(Subscription.user_id == self.owner.id).delete()
                self.db.add(
                    Subscription(
                        user_id=self.owner.id,
                        plan=plan_id,
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

                for feature in expectations["allowed"]:
                    self.assertIn(feature, ESTATE_PLAN_FEATURE_LEVELS[plan_id])
                    require_subscription_feature(self.db, self.owner.id, feature, user_role="estate")
                for feature in expectations["rejected"]:
                    with self.assertRaises(AppException, msg=f"{plan_id} should reject {feature}"):
                        require_subscription_feature(self.db, self.owner.id, feature, user_role="estate")

    def test_estate_plan_house_overage_amounts_for_requested_thresholds(self):
        scenarios = [
            ("estate_starter", 9, 17500, 1, 2500),
            ("estate_basic", 31, 28500, 1, 3500),
            ("estate_plus", 51, 49500, 1, 4500),
            ("estate_growth", 101, 91000, 1, 6000),
            ("estate_growth", 105, 115000, 5, 6000),
        ]

        for plan_id, house_count, expected_total, expected_extra_houses, expected_extra_amount in scenarios:
            with self.subTest(plan=plan_id, house_count=house_count):
                self.db.query(Home).delete()
                self.db.query(User).filter(User.role == UserRole.homeowner).delete()
                for idx in range(house_count):
                    resident = User(
                        id=str(uuid.uuid4()),
                        full_name=f"{plan_id} Resident {idx}",
                        email=f"{plan_id}-{idx}@example.com",
                        password_hash="hashed",
                        role=UserRole.homeowner,
                        email_verified=True,
                        estate_id=self.estate.id,
                    )
                    self.db.add(resident)
                    self.db.flush()
                    self.db.add(Home(name=f"{plan_id} Unit {idx}", estate_id=self.estate.id, homeowner_id=resident.id))
                self.db.commit()

                billing = calculate_plan_billing_amount(
                    self.db,
                    user_id=self.owner.id,
                    plan=get_plan_or_raise(self.db, plan_id),
                    billing_cycle="monthly",
                )

                self.assertEqual(billing["extraHouses"], expected_extra_houses)
                self.assertEqual(billing["extraHouseAmount"], expected_extra_amount)
                self.assertEqual(billing["monthlyTotal"], expected_total)

    def test_estate_plan_included_house_boundaries_have_no_overage(self):
        scenarios = [
            ("estate_starter", 8, 15000),
            ("estate_basic", 30, 25000),
            ("estate_plus", 50, 45000),
            ("estate_growth", 100, 85000),
        ]

        for plan_id, house_count, expected_total in scenarios:
            with self.subTest(plan=plan_id, house_count=house_count):
                self.db.query(Home).delete()
                self.db.query(User).filter(User.role == UserRole.homeowner).delete()
                for idx in range(house_count):
                    resident = self._add_resident(f"{plan_id}-included-{idx}@example.com")
                    self.db.add(Home(name=f"{plan_id} Included Unit {idx}", estate_id=self.estate.id, homeowner_id=resident.id))
                self.db.commit()

                billing = calculate_plan_billing_amount(
                    self.db,
                    user_id=self.owner.id,
                    plan=get_plan_or_raise(self.db, plan_id),
                    billing_cycle="monthly",
                )

                self.assertEqual(billing["activeHouseCount"], house_count)
                self.assertEqual(billing["extraHouses"], 0)
                self.assertEqual(billing["monthlyTotal"], expected_total)

    def test_current_home_lifecycle_counts_registered_estate_home_rows(self):
        self.db.add(Home(name="Empty Unit", estate_id=self.estate.id, homeowner_id=self.owner.id))
        resident = self._add_resident("lifecycle-resident@example.com")
        self.db.add(Home(name="Occupied Unit", estate_id=self.estate.id, homeowner_id=resident.id))
        self.db.commit()

        billing = calculate_plan_billing_amount(
            self.db,
            user_id=self.owner.id,
            plan=get_plan_or_raise(self.db, "estate_starter"),
            billing_cycle="monthly",
        )

        self.assertEqual(billing["activeHouseCount"], 2)
        self.assertEqual(billing["monthlyTotal"], 15000)

    def test_estate_resident_inherits_estate_subscription_features(self):
        self.db.add(
            Subscription(
                user_id=self.owner.id,
                plan="estate_plus",
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
        resident = self._add_resident("inherited-resident@example.com")
        self.db.add(Home(name="Inherited Unit", estate_id=self.estate.id, homeowner_id=resident.id))
        self.db.commit()

        subscription = get_effective_subscription(self.db, resident.id, user_role="homeowner")

        self.assertTrue(subscription["managedByEstate"])
        self.assertEqual(subscription["subscriptionOwnerId"], self.owner.id)
        self.assertEqual(subscription["plan"], "estate_plus")
        self.assertTrue(subscription["featureFlags"]["visitor_scheduling"])
        self.assertFalse(subscription["isBillPayer"])

    def test_starter_estate_resident_can_create_basic_visitor_pass_not_schedule(self):
        self.db.add(
            Subscription(
                user_id=self.owner.id,
                plan="estate_starter",
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
        resident = self._add_resident("starter-pass-resident@example.com")
        home = Home(name="Starter Pass Unit", estate_id=self.estate.id, homeowner_id=resident.id)
        self.db.add(home)
        self.db.flush()
        door = Door(name="Main Gate", home_id=home.id)
        self.db.add(door)
        self.db.commit()

        created = create_homeowner_access_pass(
            self.db,
            homeowner_id=resident.id,
            label="Guest",
            pass_type="qr",
            visitor_name="Guest Visitor",
            door_id=door.id,
        )

        self.assertEqual(created["visitorName"], "Guest Visitor")
        with self.assertRaises(AppException):
            require_subscription_feature(self.db, resident.id, "visitor_scheduling", user_role="homeowner")

    def test_billable_house_count_ignores_residents_qr_passes_guards_and_gates(self):
        homeowner = self._add_resident("unit-owner@example.com")
        home = Home(name="Unit A12", estate_id=self.estate.id, homeowner_id=homeowner.id)
        self.db.add(home)
        self.db.flush()

        for idx in range(5):
            self._add_resident(f"resident-extra-{idx}@example.com")
        for idx in range(10):
            self.db.add(
                User(
                    id=str(uuid.uuid4()),
                    full_name=f"Guard {idx}",
                    email=f"guard-{idx}@example.com",
                    password_hash="hashed",
                    role=UserRole.security,
                    email_verified=True,
                    estate_id=self.estate.id,
                )
            )
        for idx in range(3):
            self.db.add(Door(name=f"Gate {idx}", home_id=home.id))
        for idx in range(100):
            self.db.add(
                DigitalAccessPass(
                    homeowner_id=homeowner.id,
                    estate_id=self.estate.id,
                    home_id=home.id,
                    code_value=f"visitor-pass-{idx}",
                    valid_until=utc_now() + timedelta(days=1),
                )
            )
        self.db.add(
            QRCode(
                qr_id="qr-house-a12",
                plan="single",
                home_id=home.id,
                doors_csv="",
                mode=f"house:{home.id}",
                estate_id=self.estate.id,
                active=True,
            )
        )
        self.db.commit()

        billing = calculate_plan_billing_amount(
            self.db,
            user_id=self.owner.id,
            plan=get_plan_or_raise(self.db, "estate_starter"),
            billing_cycle="monthly",
        )

        self.assertEqual(billing["activeHouseCount"], 1)
        self.assertEqual(billing["extraHouses"], 0)
        self.assertEqual(billing["monthlyTotal"], 15000)

    def test_house_qr_regeneration_does_not_change_billable_house_count(self):
        homeowner = self._add_resident("regenerate-owner@example.com")
        home = Home(name="Unit B7", estate_id=self.estate.id, homeowner_id=homeowner.id)
        self.db.add(home)
        self.db.flush()
        for idx in range(4):
            self.db.add(
                QRCode(
                    qr_id=f"qr-regenerated-{idx}",
                    plan="single",
                    home_id=home.id,
                    doors_csv="",
                    mode=f"house:{home.id}:v{idx}",
                    estate_id=None,
                    active=idx == 3,
                )
            )
        self.db.commit()

        billing = calculate_plan_billing_amount(
            self.db,
            user_id=self.owner.id,
            plan=get_plan_or_raise(self.db, "estate_starter"),
            billing_cycle="monthly",
        )

        self.assertEqual(billing["activeHouseCount"], 1)
        self.assertEqual(billing["monthlyTotal"], 15000)

    def test_plus_capacity_ignores_large_visitor_resident_security_and_gate_volume(self):
        for idx in range(50):
            homeowner = self._add_resident(f"plus-owner-{idx}@example.com")
            home = Home(name=f"Plus Unit {idx}", estate_id=self.estate.id, homeowner_id=homeowner.id)
            self.db.add(home)
            self.db.flush()
            self.db.add(Door(name=f"Gate Main {idx}", home_id=home.id))
            self.db.add(
                QRCode(
                    qr_id=f"qr-plus-house-{idx}",
                    plan="single",
                    home_id=home.id,
                    doors_csv="",
                    mode=f"house:{home.id}",
                    estate_id=self.estate.id,
                    active=True,
                )
            )
        for idx in range(100):
            self._add_resident(f"plus-extra-resident-{idx}@example.com")
        for idx in range(20):
            self.db.add(
                User(
                    id=str(uuid.uuid4()),
                    full_name=f"Plus Guard {idx}",
                    email=f"plus-guard-{idx}@example.com",
                    password_hash="hashed",
                    role=UserRole.security,
                    email_verified=True,
                    estate_id=self.estate.id,
                )
            )
        for idx in range(2000):
            self.db.add(
                DigitalAccessPass(
                    homeowner_id=self.owner.id,
                    estate_id=self.estate.id,
                    code_value=f"plus-visitor-pass-{idx}",
                    valid_until=utc_now() + timedelta(days=1),
                )
            )
        self.db.commit()

        billing = calculate_plan_billing_amount(
            self.db,
            user_id=self.owner.id,
            plan=get_plan_or_raise(self.db, "estate_plus"),
            billing_cycle="monthly",
        )

        self.assertEqual(billing["activeHouseCount"], 50)
        self.assertEqual(billing["extraHouses"], 0)
        self.assertEqual(billing["monthlyTotal"], 45000)

    def test_plus_overage_counts_only_the_extra_house(self):
        for idx in range(51):
            homeowner = self._add_resident(f"plus-overage-owner-{idx}@example.com")
            home = Home(name=f"Plus Overage Unit {idx}", estate_id=self.estate.id, homeowner_id=homeowner.id)
            self.db.add(home)
            self.db.flush()
            self.db.add(
                QRCode(
                    qr_id=f"qr-plus-overage-house-{idx}",
                    plan="single",
                    home_id=home.id,
                    doors_csv="",
                    mode=f"house:{home.id}",
                    estate_id=self.estate.id,
                    active=True,
                )
            )
        for idx in range(149):
            self._add_resident(f"plus-overage-extra-resident-{idx}@example.com")
        for idx in range(5000):
            self.db.add(
                DigitalAccessPass(
                    homeowner_id=self.owner.id,
                    estate_id=self.estate.id,
                    code_value=f"plus-overage-visitor-pass-{idx}",
                    valid_until=utc_now() + timedelta(days=1),
                )
            )
        self.db.commit()

        billing = calculate_plan_billing_amount(
            self.db,
            user_id=self.owner.id,
            plan=get_plan_or_raise(self.db, "estate_plus"),
            billing_cycle="monthly",
        )

        self.assertEqual(billing["activeHouseCount"], 51)
        self.assertEqual(billing["extraHouses"], 1)
        self.assertEqual(billing["extraHouseAmount"], 4500)
        self.assertEqual(billing["monthlyTotal"], 49500)

    def _add_resident(self, email: str) -> User:
        resident = User(
            id=str(uuid.uuid4()),
            full_name=email.split("@")[0],
            email=email,
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
            estate_id=self.estate.id,
        )
        self.db.add(resident)
        self.db.flush()
        return resident


if __name__ == "__main__":
    unittest.main()
