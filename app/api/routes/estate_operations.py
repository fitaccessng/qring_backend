from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.services.advanced_service import create_snapshot_audit
from app.services.estate_operations_service import (
    clock_guard,
    create_blocklist_entry,
    create_incident,
    create_package,
    create_vehicle,
    deactivate_blocklist_entry,
    list_blocklist,
    list_guard_attendance,
    get_incident_detail,
    list_incidents,
    list_packages,
    list_vehicles,
    record_vehicle_gate_action,
    update_package_status,
)

router = APIRouter()
settings = get_settings()


class VehicleCreatePayload(BaseModel):
    plateNumber: str
    vehicleType: str = "car"
    makeModel: str | None = None
    color: str | None = None
    homeId: str | None = None


class VehicleGatePayload(BaseModel):
    action: str


class BlocklistCreatePayload(BaseModel):
    visitorName: str
    visitorPhone: str | None = None
    reason: str = ""


class PackageCreatePayload(BaseModel):
    homeId: str
    courier: str | None = None
    description: str = ""


class PackageStatusPayload(BaseModel):
    status: str


class AttendancePayload(BaseModel):
    action: str


class IncidentCreatePayload(BaseModel):
    incidentType: str = "general"
    description: str
    severity: str = "medium"
    photoUrl: str | None = None
    relatedVisitorSessionId: str | None = None


@router.get("/vehicles")
def estate_ops_list_vehicles(
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "homeowner", "security")),
):
    return {"data": list_vehicles(db, actor=user, query=q)}


@router.post("/vehicles")
def estate_ops_create_vehicle(
    payload: VehicleCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "homeowner")),
):
    return {
        "data": create_vehicle(
            db,
            actor=user,
            plate_number=payload.plateNumber,
            vehicle_type=payload.vehicleType,
            make_model=payload.makeModel,
            color=payload.color,
            home_id=payload.homeId,
        )
    }


@router.post("/vehicles/{vehicle_id}/gate")
def estate_ops_vehicle_gate_action(
    vehicle_id: str,
    payload: VehicleGatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    return {"data": record_vehicle_gate_action(db, actor=user, vehicle_id=vehicle_id, action=payload.action)}


@router.get("/blocklist")
def estate_ops_blocklist(
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "security")),
):
    return {"data": list_blocklist(db, actor=user, query=q)}


@router.post("/blocklist")
def estate_ops_create_blocklist(
    payload: BlocklistCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "security")),
):
    return {"data": create_blocklist_entry(db, actor=user, visitor_name=payload.visitorName, visitor_phone=payload.visitorPhone, reason=payload.reason)}


@router.post("/blocklist/{entry_id}/deactivate")
def estate_ops_deactivate_blocklist(
    entry_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "security")),
):
    return {"data": deactivate_blocklist_entry(db, actor=user, entry_id=entry_id)}


@router.get("/packages")
def estate_ops_list_packages(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "homeowner", "security")),
):
    return {"data": list_packages(db, actor=user, status=status)}


@router.post("/packages")
def estate_ops_create_package(
    payload: PackageCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    return {"data": create_package(db, actor=user, home_id=payload.homeId, courier=payload.courier, description=payload.description)}


@router.post("/packages/{package_id}/status")
def estate_ops_package_status(
    package_id: str,
    payload: PackageStatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "homeowner", "security")),
):
    return {"data": update_package_status(db, actor=user, package_id=package_id, status=payload.status)}


@router.get("/guard-attendance")
def estate_ops_list_guard_attendance(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "security")),
):
    return {"data": list_guard_attendance(db, actor=user)}


@router.post("/guard-attendance")
def estate_ops_guard_attendance(
    payload: AttendancePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    return {"data": clock_guard(db, actor=user, action=payload.action)}


@router.get("/incidents")
def estate_ops_list_incidents(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "security")),
):
    return {"data": list_incidents(db, actor=user, status=status)}


@router.post("/incidents")
def estate_ops_create_incident(
    payload: IncidentCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "security")),
):
    return {
        "data": create_incident(
            db,
            actor=user,
            incident_type=payload.incidentType,
            description=payload.description,
            severity=payload.severity,
            photo_url=payload.photoUrl,
            related_visitor_session_id=payload.relatedVisitorSessionId,
        )
    }


@router.get("/incidents/{incident_id}")
def estate_ops_get_incident(
    incident_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "security")),
):
    return {"data": get_incident_detail(db, actor=user, incident_id=incident_id)}


@router.post("/incidents/photo")
async def estate_ops_upload_incident_photo(
    media: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("security")),
):
    content_type = (media.content_type or "").strip().lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise AppException("Incident photo must be a JPEG, PNG, or WebP image.", status_code=400)
    media_bytes = await media.read()
    max_bytes = max(1, int(getattr(settings, "MAX_VISITOR_SNAPSHOT_BYTES", 3 * 1024 * 1024) or 3 * 1024 * 1024))
    if not media_bytes:
        raise AppException("Incident photo is empty.", status_code=400)
    if len(media_bytes) > max_bytes:
        raise AppException("Incident photo is too large. Please choose a smaller image.", status_code=400)
    data = create_snapshot_audit(
        db,
        homeowner_id=user.id,
        media_bytes=media_bytes,
        filename_hint=media.filename or "incident-photo.jpg",
        media_type="photo",
        source="security_incident",
    )
    return {"data": {"photoUrl": data.get("fileUrl") or data.get("url"), "snapshotAuditId": data.get("id")}}
