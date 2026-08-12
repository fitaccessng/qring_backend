from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.core.time import utc_now
from app.db.models import (
    Estate,
    EstatePackage,
    GateLog,
    GuardAttendance,
    Home,
    Notification,
    ResidentVehicle,
    SecurityIncident,
    User,
    UserRole,
    VisitorBlocklistEntry,
)
from app.services.payment_service import require_subscription_feature


def _estate_for_actor(db: Session, actor: User, estate_id: str | None = None) -> Estate:
    if actor.role == UserRole.estate:
        q = db.query(Estate).filter(Estate.owner_id == actor.id)
        if estate_id:
            q = q.filter(Estate.id == estate_id)
        estate = q.order_by(Estate.created_at.desc()).first()
    elif actor.role == UserRole.security:
        estate = db.query(Estate).filter(Estate.id == actor.estate_id).first() if actor.estate_id else None
    elif actor.role == UserRole.homeowner:
        row = (
            db.query(Home, Estate)
            .join(Estate, Estate.id == Home.estate_id)
            .filter(Home.homeowner_id == actor.id, Home.estate_id.is_not(None))
            .order_by(Home.created_at.desc())
            .first()
        )
        estate = row[1] if row else None
    else:
        estate = None
    if not estate:
        raise AppException("Estate not found for this account", status_code=404)
    return estate


def _home_for_actor(db: Session, actor: User, home_id: str | None = None) -> Home:
    q = db.query(Home).filter(Home.homeowner_id == actor.id)
    if home_id:
        q = q.filter(Home.id == home_id)
    home = q.order_by(Home.created_at.desc()).first()
    if not home or not home.estate_id:
        raise AppException("Estate home not found for this account", status_code=404)
    return home


def _home_in_estate(db: Session, estate_id: str, home_id: str) -> Home:
    home = db.query(Home).filter(Home.id == home_id, Home.estate_id == estate_id).first()
    if not home:
        raise AppException("Home not found in this estate", status_code=404)
    return home


def _serialize_vehicle(row: ResidentVehicle, home: Home | None = None, resident: User | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "estateId": row.estate_id,
        "homeId": row.home_id,
        "homeName": home.name if home else "",
        "residentId": row.resident_id,
        "residentName": resident.full_name if resident else "",
        "plateNumber": row.plate_number,
        "vehicleType": row.vehicle_type,
        "makeModel": row.make_model,
        "color": row.color,
        "active": bool(row.is_active),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def create_vehicle(db: Session, *, actor: User, plate_number: str, vehicle_type: str = "car", make_model: str | None = None, color: str | None = None, home_id: str | None = None) -> dict[str, Any]:
    if actor.role == UserRole.homeowner:
        home = _home_for_actor(db, actor, home_id=home_id)
    else:
        if not home_id:
            raise AppException("homeId is required", status_code=400)
        home = _home_in_estate(db, _estate_for_actor(db, actor).id, home_id)
    estate = db.query(Estate).filter(Estate.id == home.estate_id).first()
    require_subscription_feature(db, estate.owner_id, "vehicle_registration", user_role="estate")
    plate = (plate_number or "").strip().upper()
    if not plate:
        raise AppException("Plate number is required", status_code=400)
    row = ResidentVehicle(
        estate_id=estate.id,
        home_id=home.id,
        resident_id=home.homeowner_id,
        plate_number=plate,
        vehicle_type=(vehicle_type or "car").strip().lower(),
        make_model=(make_model or "").strip() or None,
        color=(color or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    resident = db.query(User).filter(User.id == row.resident_id).first()
    return _serialize_vehicle(row, home=home, resident=resident)


def list_vehicles(db: Session, *, actor: User, query: str | None = None) -> list[dict[str, Any]]:
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "vehicle_registration", user_role="estate")
    q = db.query(ResidentVehicle, Home, User).join(Home, Home.id == ResidentVehicle.home_id).join(User, User.id == ResidentVehicle.resident_id).filter(ResidentVehicle.estate_id == estate.id)
    if actor.role == UserRole.homeowner:
        q = q.filter(ResidentVehicle.resident_id == actor.id)
    term = (query or "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(or_(ResidentVehicle.plate_number.ilike(like), User.full_name.ilike(like), Home.name.ilike(like)))
    return [_serialize_vehicle(vehicle, home=home, resident=resident) for vehicle, home, resident in q.order_by(ResidentVehicle.created_at.desc()).limit(200).all()]


def record_vehicle_gate_action(db: Session, *, actor: User, vehicle_id: str, action: str) -> dict[str, Any]:
    if actor.role != UserRole.security:
        raise AppException("Only security can record vehicle gate activity", status_code=403)
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "vehicle_entry_exit_records", user_role="estate")
    vehicle = db.query(ResidentVehicle).filter(ResidentVehicle.id == vehicle_id, ResidentVehicle.estate_id == estate.id, ResidentVehicle.is_active.is_(True)).first()
    if not vehicle:
        raise AppException("Vehicle not found", status_code=404)
    normalized = (action or "").strip().lower()
    if normalized not in {"entry", "exit"}:
        raise AppException("Vehicle action must be entry or exit", status_code=400)
    db.add(
        GateLog(
            estate_id=estate.id,
            home_id=vehicle.home_id,
            gate_id=actor.gate_id,
            actor_user_id=actor.id,
            actor_role="security",
            action=f"vehicle_{normalized}",
            resulting_status=normalized,
            notes=f"Vehicle {vehicle.plate_number} {normalized}",
            meta_json=json.dumps({"vehicleId": vehicle.id, "plateNumber": vehicle.plate_number}),
        )
    )
    db.commit()
    return {"vehicleId": vehicle.id, "action": normalized, "status": "recorded"}


def create_blocklist_entry(db: Session, *, actor: User, visitor_name: str, visitor_phone: str | None, reason: str) -> dict[str, Any]:
    estate = _estate_for_actor(db, actor)
    if actor.role not in {UserRole.estate, UserRole.security}:
        raise AppException("Only estate admins or security can manage blocked visitors", status_code=403)
    require_subscription_feature(db, estate.owner_id, "block_unwanted_visitors", user_role="estate")
    row = VisitorBlocklistEntry(
        estate_id=estate.id,
        visitor_name=(visitor_name or "").strip(),
        visitor_phone=(visitor_phone or "").strip() or None,
        reason=(reason or "").strip(),
        created_by_user_id=actor.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "visitorName": row.visitor_name, "visitorPhone": row.visitor_phone, "reason": row.reason, "active": row.is_active}


def list_blocklist(db: Session, *, actor: User, query: str | None = None) -> list[dict[str, Any]]:
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "block_unwanted_visitors", user_role="estate")
    q = db.query(VisitorBlocklistEntry).filter(VisitorBlocklistEntry.estate_id == estate.id, VisitorBlocklistEntry.is_active.is_(True))
    term = (query or "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(or_(VisitorBlocklistEntry.visitor_name.ilike(like), VisitorBlocklistEntry.visitor_phone.ilike(like)))
    return [{"id": row.id, "visitorName": row.visitor_name, "visitorPhone": row.visitor_phone, "reason": row.reason, "active": row.is_active} for row in q.order_by(VisitorBlocklistEntry.created_at.desc()).limit(200).all()]


def deactivate_blocklist_entry(db: Session, *, actor: User, entry_id: str) -> dict[str, Any]:
    estate = _estate_for_actor(db, actor)
    if actor.role not in {UserRole.estate, UserRole.security}:
        raise AppException("Only estate admins or security can manage blocked visitors", status_code=403)
    require_subscription_feature(db, estate.owner_id, "block_unwanted_visitors", user_role="estate")
    row = db.query(VisitorBlocklistEntry).filter(VisitorBlocklistEntry.id == entry_id, VisitorBlocklistEntry.estate_id == estate.id).first()
    if not row:
        raise AppException("Blocked visitor was not found", status_code=404)
    row.is_active = False
    row.updated_at = utc_now()
    db.commit()
    return {"id": row.id, "active": False}


def assert_visitor_not_blocked(db: Session, *, estate_id: str | None, visitor_name: str | None, visitor_phone: str | None) -> None:
    if not estate_id:
        return
    name = (visitor_name or "").strip().lower()
    phone = (visitor_phone or "").strip()
    if not name and not phone:
        return
    conditions = []
    if phone:
        conditions.append(VisitorBlocklistEntry.visitor_phone == phone)
    if name:
        conditions.append(func.lower(VisitorBlocklistEntry.visitor_name) == name)
    row = (
        db.query(VisitorBlocklistEntry)
        .filter(VisitorBlocklistEntry.estate_id == estate_id, VisitorBlocklistEntry.is_active.is_(True), or_(*conditions))
        .first()
    )
    if row:
        raise AppException("This visitor is blocked from entry. Contact the estate admin.", status_code=403)


def create_package(db: Session, *, actor: User, home_id: str, courier: str | None, description: str) -> dict[str, Any]:
    if actor.role != UserRole.security:
        raise AppException("Only security can record package arrivals", status_code=403)
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "package_tracking", user_role="estate")
    home = _home_in_estate(db, estate.id, home_id)
    row = EstatePackage(estate_id=estate.id, home_id=home.id, resident_id=home.homeowner_id, courier=(courier or "").strip() or None, description=(description or "").strip(), recorded_by_user_id=actor.id, gate_id=actor.gate_id)
    db.add(row)
    db.flush()
    db.add(Notification(user_id=home.homeowner_id, kind="package.arrived", payload=json.dumps({"packageId": row.id, "message": "A package has arrived at the gate.", "courier": row.courier})))
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": row.status, "homeId": row.home_id, "residentId": row.resident_id, "courier": row.courier, "description": row.description}


def list_packages(db: Session, *, actor: User, status: str | None = None) -> list[dict[str, Any]]:
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "package_tracking", user_role="estate")
    q = db.query(EstatePackage, Home, User).join(Home, Home.id == EstatePackage.home_id).join(User, User.id == EstatePackage.resident_id).filter(EstatePackage.estate_id == estate.id)
    if actor.role == UserRole.homeowner:
        q = q.filter(EstatePackage.resident_id == actor.id)
    normalized = (status or "").strip().lower()
    if normalized:
        q = q.filter(EstatePackage.status == normalized)
    return [
        {
            "id": package.id,
            "homeId": package.home_id,
            "homeName": home.name,
            "residentId": package.resident_id,
            "residentName": resident.full_name,
            "courier": package.courier,
            "description": package.description,
            "status": package.status,
            "arrivedAt": package.arrived_at.isoformat() if package.arrived_at else None,
            "collectedAt": package.collected_at.isoformat() if package.collected_at else None,
        }
        for package, home, resident in q.order_by(EstatePackage.arrived_at.desc()).limit(200).all()
    ]


def update_package_status(db: Session, *, actor: User, package_id: str, status: str) -> dict[str, Any]:
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "package_tracking", user_role="estate")
    row = db.query(EstatePackage).filter(EstatePackage.id == package_id, EstatePackage.estate_id == estate.id).first()
    if not row:
        raise AppException("Package not found", status_code=404)
    normalized = (status or "").strip().lower()
    if normalized not in {"collected", "received"}:
        raise AppException("Package status must be collected or received", status_code=400)
    row.status = normalized
    row.collected_by_user_id = actor.id
    row.collected_at = utc_now()
    db.commit()
    return {"id": row.id, "status": row.status}


def clock_guard(db: Session, *, actor: User, action: str) -> dict[str, Any]:
    if actor.role != UserRole.security:
        raise AppException("Only security guards can clock attendance", status_code=403)
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "guard_attendance", user_role="estate")
    normalized = (action or "").strip().lower()
    if normalized == "in":
        existing = db.query(GuardAttendance).filter(GuardAttendance.guard_user_id == actor.id, GuardAttendance.status == "on_duty").first()
        if existing:
            raise AppException("Guard already has an active shift", status_code=400)
        row = GuardAttendance(estate_id=estate.id, guard_user_id=actor.id, gate_id=actor.gate_id, status="on_duty")
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"id": row.id, "status": row.status, "clockInAt": row.clock_in_at.isoformat()}
    if normalized == "out":
        row = db.query(GuardAttendance).filter(GuardAttendance.guard_user_id == actor.id, GuardAttendance.status == "on_duty").order_by(GuardAttendance.clock_in_at.desc()).first()
        if not row:
            raise AppException("No active guard shift found", status_code=400)
        row.status = "off_duty"
        row.clock_out_at = utc_now()
        db.commit()
        return {"id": row.id, "status": row.status, "clockOutAt": row.clock_out_at.isoformat()}
    raise AppException("Attendance action must be in or out", status_code=400)


def list_guard_attendance(db: Session, *, actor: User) -> list[dict[str, Any]]:
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "guard_attendance", user_role="estate")
    q = db.query(GuardAttendance, User).join(User, User.id == GuardAttendance.guard_user_id).filter(GuardAttendance.estate_id == estate.id)
    if actor.role == UserRole.security:
        q = q.filter(GuardAttendance.guard_user_id == actor.id)
    return [
        {
            "id": row.id,
            "guardId": row.guard_user_id,
            "guardName": guard.full_name,
            "gateId": row.gate_id,
            "status": row.status,
            "clockInAt": row.clock_in_at.isoformat() if row.clock_in_at else None,
            "clockOutAt": row.clock_out_at.isoformat() if row.clock_out_at else None,
        }
        for row, guard in q.order_by(GuardAttendance.clock_in_at.desc()).limit(200).all()
    ]


def create_incident(db: Session, *, actor: User, incident_type: str, description: str, severity: str = "medium", photo_url: str | None = None, related_visitor_session_id: str | None = None) -> dict[str, Any]:
    if actor.role not in {UserRole.security, UserRole.estate}:
        raise AppException("Only estate admins or security can report incidents", status_code=403)
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "incident_reporting", user_role="estate")
    row = SecurityIncident(estate_id=estate.id, reported_by_user_id=actor.id, incident_type=(incident_type or "general").strip().lower(), severity=(severity or "medium").strip().lower(), description=(description or "").strip(), gate_id=actor.gate_id, photo_url=(photo_url or "").strip() or None, related_visitor_session_id=related_visitor_session_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "type": row.incident_type, "severity": row.severity, "description": row.description, "photoUrl": row.photo_url, "status": row.status}


def list_incidents(db: Session, *, actor: User, status: str | None = None) -> list[dict[str, Any]]:
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "incident_reporting", user_role="estate")
    q = db.query(SecurityIncident, User).join(User, User.id == SecurityIncident.reported_by_user_id).filter(SecurityIncident.estate_id == estate.id)
    normalized = (status or "").strip().lower()
    if normalized:
        q = q.filter(SecurityIncident.status == normalized)
    return [
        {
            "id": row.id,
            "type": row.incident_type,
            "severity": row.severity,
            "description": row.description,
            "photoUrl": row.photo_url,
            "status": row.status,
            "reportedById": row.reported_by_user_id,
            "reportedByName": reporter.full_name,
            "gateId": row.gate_id,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        }
        for row, reporter in q.order_by(SecurityIncident.created_at.desc()).limit(200).all()
    ]


def get_incident_detail(db: Session, *, actor: User, incident_id: str) -> dict[str, Any]:
    estate = _estate_for_actor(db, actor)
    require_subscription_feature(db, estate.owner_id, "incident_reporting", user_role="estate")
    row = db.query(SecurityIncident, User).join(User, User.id == SecurityIncident.reported_by_user_id).filter(SecurityIncident.id == incident_id, SecurityIncident.estate_id == estate.id).first()
    if not row:
        raise AppException("Incident not found", status_code=404)
    incident, reporter = row
    if actor.role == UserRole.security and actor.estate_id != incident.estate_id:
        raise AppException("Not authorized to view this incident", status_code=403)
    return {
        "id": incident.id,
        "type": incident.incident_type,
        "severity": incident.severity,
        "description": incident.description,
        "photoUrl": incident.photo_url,
        "status": incident.status,
        "reportedById": incident.reported_by_user_id,
        "reportedByName": reporter.full_name,
        "gateId": incident.gate_id,
        "relatedVisitorSessionId": incident.related_visitor_session_id,
        "createdAt": incident.created_at.isoformat() if incident.created_at else None,
        "updatedAt": incident.updated_at.isoformat() if incident.updated_at else None,
    }
