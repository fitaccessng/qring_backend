from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.services.estate_service import (
    add_estate_door,
    add_home,
    assign_door_to_homeowner,
    create_estate,
    create_estate_homeowner,
    create_estate_shared_selector_qr,
    get_estate_plan_restrictions,
    invite_homeowner,
    list_estate_access_logs,
    list_estate_shared_selector_qrs,
    list_estate_mappings,
    list_estate_overview,
    provision_estate_door_with_homeowner,
    update_estate_door_admin_profile,
)

router = APIRouter()


class EstateCreate(BaseModel):
    name: str


class HomeCreate(BaseModel):
    name: str
    estateId: str | None = None
    homeownerId: str


class EstateHomeownerCreate(BaseModel):
    estateId: str
    fullName: str
    username: str
    password: str


class EstateDoorCreate(BaseModel):
    estateId: str
    homeId: str
    name: str
    generateQr: bool = True
    mode: str = "direct"
    plan: str = "single"


class EstateProvisionDoorCreate(BaseModel):
    estateId: str
    homeName: str
    doorName: str
    homeownerFullName: str
    homeownerUsername: str
    homeownerPassword: str


class DoorAssignPayload(BaseModel):
    homeownerId: str


class EstateSharedQrCreatePayload(BaseModel):
    estateId: str


class DoorAdminProfileUpdatePayload(BaseModel):
    doorName: str | None = None
    homeownerName: str | None = None
    homeownerEmail: str | None = None
    newPassword: str | None = None


@router.post("/")
def estate_create(
    payload: EstateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    estate = create_estate(db, payload.name, owner_id=user.id)
    return {"data": {"id": estate.id, "name": estate.name}}


@router.post("/homes")
def estate_add_home(
    payload: HomeCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    home = add_home(db, payload.name, payload.estateId, payload.homeownerId, owner_id=user.id)
    return {"data": {"id": home.id, "name": home.name}}


@router.get("/overview")
def estate_overview(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    return {"data": list_estate_overview(db, owner_id=user.id)}


@router.post("/homeowners")
def estate_create_homeowner(
    payload: EstateHomeownerCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    homeowner = create_estate_homeowner(
        db=db,
        owner_id=user.id,
        estate_id=payload.estateId,
        full_name=payload.fullName,
        username=payload.username,
        password=payload.password,
    )
    return {
        "data": {
            "id": homeowner.id,
            "fullName": homeowner.full_name,
            "email": homeowner.email,
            "username": payload.username,
        }
    }


@router.post("/doors")
def estate_create_door(
    payload: EstateDoorCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = add_estate_door(
        db=db,
        owner_id=user.id,
        estate_id=payload.estateId,
        home_id=payload.homeId,
        door_name=payload.name,
        generate_qr=payload.generateQr,
        mode=payload.mode,
        plan=payload.plan,
    )
    return {"data": data}


@router.post("/shared-qr")
def estate_create_shared_qr(
    payload: EstateSharedQrCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = create_estate_shared_selector_qr(
        db=db,
        owner_id=user.id,
        estate_id=payload.estateId,
    )
    return {"data": data}


@router.get("/shared-qr")
def estate_list_shared_qr(
    estateId: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    return {"data": list_estate_shared_selector_qrs(db=db, owner_id=user.id, estate_id=estateId)}


@router.post("/doors/provision")
def estate_provision_door(
    payload: EstateProvisionDoorCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = provision_estate_door_with_homeowner(
        db=db,
        owner_id=user.id,
        estate_id=payload.estateId,
        home_name=payload.homeName,
        door_name=payload.doorName,
        homeowner_full_name=payload.homeownerFullName,
        homeowner_username=payload.homeownerUsername,
        homeowner_password=payload.homeownerPassword,
    )
    return {"data": data}


@router.post("/doors/{door_id}/assign-homeowner")
def estate_assign_door(
    door_id: str,
    payload: DoorAssignPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = assign_door_to_homeowner(
        db=db,
        owner_id=user.id,
        door_id=door_id,
        homeowner_id=payload.homeownerId,
    )
    return {"data": data}


@router.put("/doors/{door_id}/admin-profile")
def estate_update_door_admin_profile(
    door_id: str,
    payload: DoorAdminProfileUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = update_estate_door_admin_profile(
        db=db,
        owner_id=user.id,
        door_id=door_id,
        door_name=payload.doorName,
        homeowner_name=payload.homeownerName,
        homeowner_email=payload.homeownerEmail,
        new_password=payload.newPassword,
    )
    return {"data": data}


@router.post("/homeowners/{homeowner_id}/invite")
def estate_invite_homeowner(
    homeowner_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = invite_homeowner(db=db, owner_id=user.id, homeowner_id=homeowner_id)
    return {"data": data}


@router.get("/mappings")
def estate_mappings(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    return {"data": list_estate_mappings(db=db, owner_id=user.id)}


@router.get("/access-logs")
def estate_access_logs(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    return {"data": list_estate_access_logs(db=db, owner_id=user.id)}


@router.get("/plan-restrictions")
def estate_plan_restrictions(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    return {"data": get_estate_plan_restrictions(db=db, owner_id=user.id)}
