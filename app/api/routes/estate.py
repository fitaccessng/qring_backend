from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User
from app.db.session import get_db
from app.services.estate_alert_service import (
    create_estate_alert,
    list_estate_alert_payment_overview,
    list_estate_alerts,
    list_homeowner_alerts,
    list_maintenance_status_audits,
    record_meeting_response,
    record_poll_vote,
    send_payment_reminders,
    verify_estate_alert_payment,
    update_estate_alert,
    delete_estate_alert,
)
from app.services.estate_service import (
    add_estate_door,
    add_home,
    assign_door_to_homeowner,
    create_estate,
    create_estate_homeowner,
    create_estate_shared_selector_qr,
    get_estate_settings,
    get_estate_plan_restrictions,
    get_estate_stats_summary,
    invite_homeowner,
    list_estate_access_logs,
    list_estate_shared_selector_qrs,
    list_estate_mappings,
    list_estate_overview,
    provision_estate_door_with_homeowner,
    update_estate_door_admin_profile,
    update_estate_settings,
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


class EstateAlertCreatePayload(BaseModel):
    estateId: str
    title: str
    description: str = ""
    alertType: str
    amountDue: float | None = None
    dueDate: str | None = None
    pollOptions: list[str] | None = None
    targetHomeownerIds: list[str] | None = None


class MeetingResponsePayload(BaseModel):
    response: str


class PollVotePayload(BaseModel):
    optionIndex: int


class EstateSettingsPayload(BaseModel):
    reminderFrequencyDays: int


class EstatePaymentVerifyPayload(BaseModel):
    homeownerId: str
    paymentMethod: str | None = None
    reference: str | None = None
    receiptUrl: str | None = None


class EstateAlertUpdatePayload(BaseModel):
    title: str
    description: str = ""
    targetHomeownerIds: list[str] | None = None
    amountDue: float | None = None
    dueDate: str | None = None
    pollOptions: list[str] | None = None
    maintenanceStatus: str | None = None


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


@router.get("/{estate_id}/settings")
def estate_get_settings(
    estate_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = get_estate_settings(db=db, estate_id=estate_id, owner_id=user.id)
    return {"data": data}


@router.put("/{estate_id}/settings")
def estate_update_settings(
    estate_id: str,
    payload: EstateSettingsPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = update_estate_settings(
        db=db,
        estate_id=estate_id,
        owner_id=user.id,
        reminder_frequency_days=payload.reminderFrequencyDays,
    )
    return {"data": data}


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


@router.get("/stats-summary")
def estate_stats_summary(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    return {"data": get_estate_stats_summary(db=db, owner_id=user.id)}


@router.post("/alerts")
def estate_create_alert(
    payload: EstateAlertCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    due_date = None
    if payload.dueDate:
        try:
            due_date = datetime.fromisoformat(payload.dueDate.replace("Z", "+00:00"))
        except Exception:
            due_date = None

    data = create_estate_alert(
        db=db,
        estate_id=payload.estateId,
        estate_admin_id=user.id,
        title=payload.title,
        description=payload.description,
        alert_type=payload.alertType,
        amount_due=payload.amountDue,
        due_date=due_date,
        poll_options=payload.pollOptions,
        target_homeowner_ids=payload.targetHomeownerIds,
    )
    return {"data": data}


@router.get("/{estate_id}/alerts")
def estate_alerts_list(
    estate_id: str,
    alert_type: str | None = Query(default=None, alias="alertType"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "homeowner")),
):
    data = list_estate_alerts(
        db=db,
        estate_id=estate_id,
        actor_id=user.id,
        actor_role=user.role,
        alert_type=alert_type,
    )
    return {"data": data}


@router.get("/alerts/me")
def estate_alerts_me(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    return {"data": list_homeowner_alerts(db, homeowner_id=user.id)}


@router.put("/alerts/{alert_id}")
def estate_alert_update(
    alert_id: str,
    payload: EstateAlertUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    due_date = None
    if payload.dueDate:
        try:
            due_date = datetime.fromisoformat(payload.dueDate.replace("Z", "+00:00"))
        except Exception:
            due_date = None

    data = update_estate_alert(
        db=db,
        alert_id=alert_id,
        estate_admin_id=user.id,
        title=payload.title,
        description=payload.description,
        target_homeowner_ids=payload.targetHomeownerIds,
        amount_due=payload.amountDue,
        due_date=due_date,
        poll_options=payload.pollOptions,
        maintenance_status=payload.maintenanceStatus,
    )
    return {"data": data}


@router.delete("/alerts/{alert_id}")
def estate_alert_delete(
    alert_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    data = delete_estate_alert(db=db, alert_id=alert_id, estate_admin_id=user.id)
    return {"data": data}


@router.post("/alerts/{alert_id}/meeting-response")
def estate_meeting_response(
    alert_id: str,
    payload: MeetingResponsePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = record_meeting_response(db=db, alert_id=alert_id, homeowner_id=user.id, response=payload.response)
    return {"data": data}


@router.post("/alerts/{alert_id}/poll-vote")
def estate_poll_vote(
    alert_id: str,
    payload: PollVotePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("homeowner")),
):
    data = record_poll_vote(db=db, alert_id=alert_id, homeowner_id=user.id, option_index=payload.optionIndex)
    return {"data": data}


@router.post("/alerts/{alert_id}/remind")
def estate_alert_remind(
    alert_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate")),
):
    data = send_payment_reminders(db=db, alert_id=alert_id, estate_admin_id=user.id)
    return {"data": data}


@router.post("/alerts/{alert_id}/payments/verify")
def estate_alert_verify_payment(
    alert_id: str,
    payload: EstatePaymentVerifyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate")),
):
    data = verify_estate_alert_payment(
        db=db,
        alert_id=alert_id,
        estate_admin_id=user.id,
        homeowner_id=payload.homeownerId,
        payment_method=payload.paymentMethod,
        reference=payload.reference,
        receipt_url=payload.receiptUrl,
    )
    return {"data": data}


@router.get("/{estate_id}/alerts/payments")
def estate_alerts_payment_overview(
    estate_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate")),
):
    return {"data": list_estate_alert_payment_overview(db, estate_id=estate_id, estate_admin_id=user.id)}


@router.get("/{estate_id}/maintenance/audits")
def estate_maintenance_audits(
    estate_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("estate", "admin")),
):
    return {"data": list_maintenance_status_audits(db=db, estate_id=estate_id, estate_admin_id=user.id)}
