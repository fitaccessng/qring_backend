from __future__ import annotations

import os
from pathlib import Path

from app.core.security import hash_password
from app.core.time import utc_now
from app.db.base import Base
from app.db.models import Door, Estate, Home, ResidentVehicle, Subscription, User, UserRole
from app.db.session import SessionLocal, engine
from app.services.payment_service import _ensure_default_plans

PASSWORD = "Password123!"

USERS = {
    "manager": ("e2e.manager@qring-e2e.com", UserRole.estate, "E2E Estate Manager"),
    "starter_resident": ("e2e.starter.resident@qring-e2e.com", UserRole.homeowner, "Starter Resident"),
    "starter_guard": ("e2e.starter.guard@qring-e2e.com", UserRole.security, "Starter Guard"),
    "other_guard": ("e2e.other.guard@qring-e2e.com", UserRole.security, "Other Estate Guard"),
    "basic_resident": ("e2e.basic.resident@qring-e2e.com", UserRole.homeowner, "Basic Resident"),
    "basic_guard": ("e2e.basic.guard@qring-e2e.com", UserRole.security, "Basic Guard"),
    "plus_resident": ("e2e.plus.resident@qring-e2e.com", UserRole.homeowner, "Plus Resident"),
    "plus_guard": ("e2e.plus.guard@qring-e2e.com", UserRole.security, "Plus Guard"),
}


def _guarded_reset() -> None:
    database_url = os.getenv("DATABASE_URL", "")
    if os.getenv("QRING_E2E_SEED") != "1":
        raise SystemExit("Refusing to seed without QRING_E2E_SEED=1")
    if not database_url.startswith("sqlite:///") or not any(token in database_url.lower() for token in ("e2e", "test")):
        raise SystemExit(f"Refusing to seed non-test database: {database_url}")

    db_path = Path(database_url.replace("sqlite:///", "", 1))
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def _user(db, key: str) -> User:
    email, role, name = USERS[key]
    row = User(
        full_name=name,
        email=email,
        password_hash=hash_password(PASSWORD),
        role=role,
        is_active=True,
        email_verified=True,
        phone="08030000000",
    )
    db.add(row)
    db.flush()
    return row


def _subscription(db, owner: User, plan: str) -> None:
    db.add(
        Subscription(
            user_id=owner.id,
            tenant_id=owner.id,
            tenant_type="estate",
            billing_scope="estate",
            plan=plan,
            status="active",
            payment_status="paid",
            starts_at=utc_now(),
            ends_at=None,
            amount_due=0,
            amount_paid=0,
        )
    )


def _estate(db, manager: User, *, name: str, plan: str, resident: User, guard: User, other_house: str, join_code: str) -> dict:
    estate = Estate(name=name, owner_id=manager.id, join_code=join_code)
    db.add(estate)
    db.flush()
    _subscription(db, manager, plan)

    home = Home(name=f"{name} House 1", estate_id=estate.id, homeowner_id=resident.id)
    spare_home = Home(name=other_house, estate_id=estate.id, homeowner_id=resident.id)
    db.add_all([home, spare_home])
    db.flush()
    door = Door(name=f"{name} Front Door", home_id=home.id, gate_label="Main Gate")
    db.add(door)
    guard.estate_id = estate.id
    guard.gate_id = "Main Gate"
    db.flush()
    return {"estate": estate, "home": home, "door": door, "guard": guard, "resident": resident}


def main() -> None:
    _guarded_reset()
    db = SessionLocal()
    try:
        _ensure_default_plans(db)
        manager = _user(db, "manager")
        starter_resident = _user(db, "starter_resident")
        starter_guard = _user(db, "starter_guard")
        other_guard = _user(db, "other_guard")
        basic_resident = _user(db, "basic_resident")
        basic_guard = _user(db, "basic_guard")
        plus_resident = _user(db, "plus_resident")
        plus_guard = _user(db, "plus_guard")

        starter = _estate(db, manager, name="E2E Starter Estate", plan="estate_starter", resident=starter_resident, guard=starter_guard, other_house="Starter House 2", join_code="E2ESTARTER")
        other = _estate(db, manager, name="E2E Other Estate", plan="estate_starter", resident=starter_resident, guard=other_guard, other_house="Other House 2", join_code="E2EOTHER")
        basic = _estate(db, manager, name="E2E Basic Estate", plan="estate_basic", resident=basic_resident, guard=basic_guard, other_house="Basic House 2", join_code="E2EBASIC")
        plus = _estate(db, manager, name="E2E Plus Estate", plan="estate_plus", resident=plus_resident, guard=plus_guard, other_house="Plus House 2", join_code="E2EPLUS")

        db.add(
            ResidentVehicle(
                estate_id=basic["estate"].id,
                home_id=basic["home"].id,
                resident_id=basic_resident.id,
                plate_number="E2E-001",
                vehicle_type="car",
                make_model="Seed Sedan",
                color="Blue",
            )
        )
        db.commit()
        print(
            {
                "password": PASSWORD,
                "manager": manager.email,
                "starterResident": starter_resident.email,
                "starterGuard": starter_guard.email,
                "otherGuard": other_guard.email,
                "basicResident": basic_resident.email,
                "basicGuard": basic_guard.email,
                "plusResident": plus_resident.email,
                "plusGuard": plus_guard.email,
                "starterDoorId": starter["door"].id,
                "basicHomeId": basic["home"].id,
                "plusHomeId": plus["home"].id,
                "otherEstateId": other["estate"].id,
            }
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
