from __future__ import annotations

import csv
import io
import json
import uuid
import secrets
from collections import Counter, defaultdict
from functools import partial
from datetime import date, datetime, timedelta
from typing import Any

from anyio import from_thread
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.core.time import utc_now
from app.db.models import (
    CallSession,
    Home,
    Message,
    Notification,
    Office,
    OfficeAttendanceLog,
    OfficeDepartment,
    OfficeMember,
    OfficeStaffConversation,
    OfficeStaffMessage,
    User,
    UserRole,
    VisitorSession,
)
from app.services.notification_service import create_notification
from app.services.provider_integrations import send_transactional_email
from app.services.realtime_notification_service import (
    build_notification_envelope,
    build_notification_idempotency_key,
    emit_dashboard_notification,
    emit_signaling_notification,
)
from app.services.call_service import end_call_session, mark_call_session_answered, mark_call_session_rejected, start_call_session
from app.services.realtime_config_service import build_webrtc_rtc_config

OFFICE_QUEUE_STATUSES = {"pending", "submitted", "received_by_security", "forwarded_to_homeowner", "assigned_to_staff"}
OFFICE_ACTIVE_STATUSES = {"approved", "active", "checked_in"}
OFFICE_FINAL_STATUSES = {"rejected", "completed", "checked_out", "cancelled", "expired"}
settings = get_settings()


OFFICE_STAFF_CAPABILITIES = [
    {
        "key": "visitor_invitations",
        "label": "Invite Visitor",
        "items": [
            "One-time access",
            "Create appointment",
            "Edit appointment",
            "Cancel appointment",
            "Reschedule appointment",
            "Repeat appointment",
        ],
    },
    {
        "key": "visitor_approvals",
        "label": "Approve Visitor",
        "items": ["Approve visitor", "Reject visitor"],
    },
    {
        "key": "communication_center",
        "label": "Chat",
        "items": ["Reception", "Security", "Other staff", "Department"],
    },
    {
        "key": "calendar",
        "label": "View Calendar",
        "items": ["Scheduled visits", "Appointments", "Reminders"],
    },
    {
        "key": "employee_profile",
        "label": "Employee Profile",
        "items": ["Name", "Department", "Floor", "Email", "Phone"],
    },
    {
        "key": "notifications",
        "label": "Notifications",
        "items": [
            "Visitor arrived",
            "Visitor approved",
            "Visitor rejected",
            "Appointment reminder",
            "Delivery",
            "Security notice",
            "Company announcements",
        ],
    },
    {
        "key": "settings",
        "label": "Settings",
        "items": [
            "Notifications",
            "Theme",
            "Language",
            "Password",
            "Two-factor authentication",
            "Privacy",
            "Devices",
            "Sessions",
        ],
    },
    {
        "key": "visitor_passes",
        "label": "Visitor Invitations",
        "items": [
            "Visitor name",
            "Phone",
            "Email",
            "Company",
            "Purpose",
            "Date",
            "Time",
            "Duration",
            "Vehicle plate",
            "Number of guests",
            "Send QR by WhatsApp",
            "Send Email",
            "Copy Link",
        ],
    },
    {
        "key": "visitor_requests",
        "label": "Visitor Requests",
        "items": [
            "Visitor photo",
            "Name",
            "Phone number",
            "Company",
            "Purpose",
            "Arrival time",
            "Gate",
            "Vehicle information",
            "Security notes",
            "Approve",
            "Reject",
            "Send Message",
            "Request More Information",
            "Call Reception",
            "Transfer to another employee",
        ],
    },
]


def _status_label(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"pending", "forwarded", "submitted", "received_by_security", "forwarded_to_homeowner"}:
        return "Awaiting approval"
    if normalized in {"assigned_to_staff"}:
        return "Assigned to staff"
    if normalized in {"approved", "active", "checked_in"}:
        return "Inside office"
    if normalized in {"rejected"}:
        return "Rejected"
    if normalized in {"completed", "checked_out", "cancelled", "expired"}:
        return normalized.replace("_", " ").title()
    return normalized.title() or "Update"


def _find_office_for_user(db: Session, user_id: str) -> Office | None:
    member = (
        db.query(OfficeMember)
        .join(Office, Office.id == OfficeMember.office_id)
        .filter(OfficeMember.user_id == user_id)
        .order_by(OfficeMember.created_at.desc())
        .first()
    )
    if member:
        return member.office
    return db.query(Office).filter(Office.administrator_user_id == user_id).first()


def _find_office_by_qr_id(db: Session, qr_id: str) -> Office | None:
    normalized_qr_id = str(qr_id or "").strip()
    if not normalized_qr_id:
        return None
    return db.query(Office).filter(Office.qr_id == normalized_qr_id, Office.active.is_(True)).first()


def generate_office_qr(db: Session, *, user_id: str) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")

    office.qr_id = f"office-{uuid.uuid4().hex[:12]}"
    db.add(office)
    db.commit()
    db.refresh(office)
    return {
        "office": _office_payload(office),
        "qrId": office.qr_id,
        "scanUrl": f"/scan/{office.qr_id}",
    }


def _office_homes_query(db: Session, office_id: str):
    return db.query(Home).filter(Home.office_id == office_id)


def _office_sessions_query(db: Session, office_id: str):
    return (
        db.query(VisitorSession)
        .join(Home, Home.id == VisitorSession.home_id)
        .filter(Home.office_id == office_id)
    )


def _office_messages_query(db: Session, office_id: str):
    return (
        db.query(Message)
        .join(VisitorSession, Message.session_id == VisitorSession.id)
        .join(Home, Home.id == VisitorSession.home_id)
        .filter(Home.office_id == office_id)
    )


def _office_room_ids(office: Office) -> list[str]:
    return [f"office:{office.id}", f"office:{office.id}:reception", f"office:{office.id}:security"]


def _office_employee_room(office_id: str, user_id: str) -> str:
    return f"office:{office_id}:employee:{user_id}"


def _serialize_office_member(member: OfficeMember) -> dict[str, Any]:
    return {
        "id": member.id,
        "userId": member.user_id,
        "name": member.full_name,
        "role": member.role_label,
        "department": member.department,
        "floor": member.floor,
        "extension": member.extension,
        "availability": member.availability_status,
        "status": member.status,
        "detailsSentAt": member.details_sent_at.isoformat() if member.details_sent_at else None,
    }


def _serialize_office_department(department: OfficeDepartment) -> dict[str, Any]:
    return {
        "id": department.id,
        "name": department.name,
        "createdAt": department.created_at.isoformat() if department.created_at else None,
    }


def _resolve_office_member_by_name(
    db: Session,
    *,
    office_id: str,
    name: str | None = None,
    department: str | None = None,
) -> OfficeMember | None:
    members = (
        db.query(OfficeMember)
        .filter(OfficeMember.office_id == office_id)
        .order_by(OfficeMember.created_at.asc())
        .all()
    )
    normalized_name = str(name or "").strip().lower()
    normalized_department = str(department or "").strip().lower()
    if not normalized_name:
        return None

    for member in members:
        member_name = str(member.full_name or "").strip().lower()
        member_department = str(member.department or "").strip().lower()
        if normalized_name == member_name and (
            not normalized_department or normalized_department == member_department or normalized_department in member_department
        ):
            return member

    for member in members:
        member_name = str(member.full_name or "").strip().lower()
        member_department = str(member.department or "").strip().lower()
        if normalized_name in member_name and (
            not normalized_department or normalized_department == member_department or normalized_department in member_department
        ):
            return member

    if normalized_department:
        for member in members:
            member_department = str(member.department or "").strip().lower()
            if normalized_department == member_department or normalized_department in member_department:
                return member
    return None


def _get_office_staff_conversation(db: Session, *, office_id: str, staff_user_id: str) -> OfficeStaffConversation:
    conversation = (
        db.query(OfficeStaffConversation)
        .filter(
            OfficeStaffConversation.office_id == office_id,
            OfficeStaffConversation.staff_user_id == staff_user_id,
        )
        .first()
    )
    if conversation:
        return conversation
    conversation = OfficeStaffConversation(
        office_id=office_id,
        staff_user_id=staff_user_id,
        created_at=utc_now(),
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def _staff_conversation_item(
    db: Session,
    *,
    office: Office,
    conversation: OfficeStaffConversation,
    current_user_id: str | None = None,
) -> dict[str, Any]:
    staff = db.query(User).filter(User.id == conversation.staff_user_id).first()
    member = (
        db.query(OfficeMember)
        .filter(OfficeMember.office_id == office.id, OfficeMember.user_id == conversation.staff_user_id)
        .first()
    )
    latest = (
        db.query(OfficeStaffMessage)
        .filter(OfficeStaffMessage.conversation_id == conversation.id)
        .order_by(OfficeStaffMessage.created_at.desc())
        .first()
    )
    unread = 0
    if current_user_id:
        unread = (
            db.query(OfficeStaffMessage.id)
            .filter(
                OfficeStaffMessage.conversation_id == conversation.id,
                OfficeStaffMessage.sender_user_id != current_user_id,
                OfficeStaffMessage.read_at.is_(None),
            )
            .count()
        )
    return {
        "id": conversation.id,
        "conversationId": conversation.id,
        "staffUserId": conversation.staff_user_id,
        "userId": conversation.staff_user_id,
        "name": member.full_name if member else (staff.full_name if staff else "Staff"),
        "displayName": member.full_name if member else (staff.full_name if staff else "Staff"),
        "role": member.role_label if member else "staff",
        "department": member.department if member else "",
        "floor": member.floor if member else "",
        "extension": member.extension if member else "",
        "status": member.status if member else "",
        "availability": member.availability_status if member else "",
        "last": latest.body if latest else "No messages yet",
        "time": latest.created_at.isoformat() if latest and latest.created_at else conversation.created_at.isoformat(),
        "unread": unread,
    }


def _build_office_staff_invite_email_body(
    *,
    office_name: str,
    staff_name: str,
    email: str,
    temporary_password: str,
    login_link: str,
    role_label: str,
) -> str:
    lines = [
        f"Hello {staff_name},",
        "",
        f"You have been added to {office_name} as {role_label}.",
        "",
        "Your account details:",
        f"Name: {staff_name}",
        f"Email: {email}",
        f"Temporary Password: {temporary_password}",
        "",
        f"Login URL: {login_link}",
        "",
        "Use these details to sign in to your office account.",
        "Please change your password after your first login.",
    ]
    return "\n".join(lines)


def _office_call_room_ids(office: Office, session_id: str | None = None, call_session_id: str | None = None) -> list[str]:
    rooms = set(_office_room_ids(office))
    if session_id:
        rooms.add(f"session:{session_id}")
    if call_session_id:
        rooms.add(f"call:{call_session_id}")
    return list(rooms)


def _office_direct_call_room_ids(
    office: Office,
    *,
    call_session_id: str | None = None,
    caller_user_id: str | None = None,
    receiver_user_id: str | None = None,
) -> list[str]:
    rooms = set(_office_room_ids(office))
    if call_session_id:
        rooms.add(f"call:{call_session_id}")
    if caller_user_id:
        rooms.add(_office_employee_room(office.id, caller_user_id))
    if receiver_user_id:
        rooms.add(_office_employee_room(office.id, receiver_user_id))
    return list(rooms)


def _office_member_or_admin_exists(db: Session, *, office_id: str, user_id: str) -> bool:
    member = (
        db.query(OfficeMember.id)
        .filter(OfficeMember.office_id == office_id, OfficeMember.user_id == user_id)
        .first()
    )
    if member:
        return True
    return bool(db.query(Office.id).filter(Office.id == office_id, Office.administrator_user_id == user_id).first())


def _office_call_target_user(
    db: Session,
    *,
    office: Office,
    target_role: str | None,
    employee_id: str | None = None,
    reception_id: str | None = None,
    security_id: str | None = None,
) -> str | None:
    normalized_role = str(target_role or "visitor").strip().lower() or "visitor"
    if normalized_role == "visitor":
        return None

    target_user_id = {
        "employee": str(employee_id or "").strip() or None,
        "reception": str(reception_id or "").strip() or None,
        "security": str(security_id or "").strip() or None,
    }.get(normalized_role)
    if normalized_role not in {"employee", "reception", "security"}:
        raise ValueError("Invalid target role")
    if not target_user_id:
        raise ValueError(f"{normalized_role}Id is required")

    if normalized_role == "employee":
        if not _office_member_or_admin_exists(db, office_id=office.id, user_id=target_user_id):
            raise ValueError("Target employee is not part of this office")
        return target_user_id

    if normalized_role in {"reception", "security"}:
        if not _office_member_or_admin_exists(db, office_id=office.id, user_id=target_user_id):
            raise ValueError(f"Target {normalized_role} is not part of this office")
        return target_user_id

    return None


def create_office_staff_member(
    db: Session,
    *,
    admin_user_id: str,
    full_name: str,
    email: str,
    role_label: str = "employee",
    department: str | None = None,
    floor: str | None = None,
    extension: str | None = None,
    availability_status: str | None = None,
    temporary_password: str | None = None,
) -> dict[str, Any]:
    office = db.query(Office).filter(Office.administrator_user_id == admin_user_id).first()
    if not office:
        raise ValueError("Office not found")

    clean_name = str(full_name or "").strip()
    clean_email = str(email or "").strip().lower()
    if not clean_name:
        raise ValueError("Staff full name is required")
    if not clean_email:
        raise ValueError("Staff email is required")

    member_count = (
        db.query(OfficeMember.id)
        .filter(OfficeMember.office_id == office.id)
        .count()
    )
    if office.employee_count > 0 and member_count >= office.employee_count:
        raise ValueError("Your office staff limit has been reached")

    existing_user = db.query(User).filter(User.email == clean_email).first()
    if existing_user and existing_user.role not in {UserRole.office, UserRole.office_staff}:
        raise ValueError("A non-office account already uses that email")

    existing_member = None
    if existing_user:
        existing_member = db.query(OfficeMember).filter(OfficeMember.user_id == existing_user.id).first()
        if existing_member and existing_member.office_id != office.id:
            raise ValueError("That account already belongs to another office")
        if existing_member and existing_member.office_id == office.id:
            raise ValueError("That staff account is already linked to this office")

    password_value = str(temporary_password or "").strip() or secrets.token_urlsafe(10)
    if existing_user:
        user = existing_user
        user.full_name = clean_name
        user.password_hash = hash_password(password_value)
        user.email_verified = True
    else:
        user = User(
            full_name=clean_name,
            email=clean_email,
            password_hash=hash_password(password_value),
            role=UserRole.office_staff,
            email_verified=True,
        )
        db.add(user)

    member = OfficeMember(
        office_id=office.id,
        user_id=user.id,
        full_name=clean_name,
        role_label=(role_label or "employee").strip() or "employee",
        department=(department or "").strip() or None,
        floor=(floor or "").strip() or None,
        extension=(extension or "").strip() or None,
        availability_status=(availability_status or "available").strip() or "available",
        status="active",
    )
    db.add(member)
    db.commit()
    db.refresh(user)
    db.refresh(member)
    create_notification(
        db=db,
        user_id=office.administrator_user_id,
        kind="office.staff.created",
        payload={
            "officeId": office.id,
            "officeName": office.company_name,
            "employeeId": user.id,
            "employeeName": clean_name,
            "email": clean_email,
            "message": f"{clean_name} was added to {office.company_name}.",
        },
    )

    return {
        "office": _office_payload(office),
        "staff": _serialize_office_member(member),
        "user": {
            "id": user.id,
            "fullName": user.full_name,
            "email": user.email,
            "role": user.role.value,
        },
        "temporaryPassword": password_value,
        "loginLink": f"{settings.FRONTEND_BASE_URL.rstrip('/')}/login",
    }


def send_office_staff_details(
    db: Session,
    *,
    admin_user_id: str,
    employee_id: str,
) -> dict[str, Any]:
    office = db.query(Office).filter(Office.administrator_user_id == admin_user_id).first()
    if not office:
        raise ValueError("Office not found")

    member = (
        db.query(OfficeMember)
        .join(User, User.id == OfficeMember.user_id)
        .filter(OfficeMember.office_id == office.id)
        .filter(OfficeMember.user_id == employee_id)
        .first()
    )
    if not member or not member.user_id:
        raise ValueError("Staff member not found")

    user = db.query(User).filter(User.id == member.user_id).first()
    if not user:
        raise ValueError("Staff user not found")

    password_value = secrets.token_urlsafe(10)
    user.full_name = member.full_name
    user.password_hash = hash_password(password_value)
    user.email_verified = True
    member.details_sent_at = utc_now()
    db.commit()
    db.refresh(user)
    db.refresh(member)

    login_link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/login"
    try:
        email_body = _build_office_staff_invite_email_body(
            office_name=office.company_name,
            staff_name=member.full_name,
            email=user.email,
            temporary_password=password_value,
            login_link=login_link,
            role_label=member.role_label,
        )
        send_transactional_email(
            to_email=user.email,
            subject=f"Welcome to {office.company_name} on Qring",
            body=email_body,
        )
    except Exception:
        pass

    create_notification(
        db=db,
        user_id=office.administrator_user_id,
        kind="office.staff.details_sent",
        payload={
            "officeId": office.id,
            "officeName": office.company_name,
            "employeeId": user.id,
            "employeeName": member.full_name,
            "email": user.email,
            "message": f"Details were sent to {member.full_name}.",
        },
    )

    return {
        "office": _office_payload(office),
        "staff": _serialize_office_member(member),
        "user": {
            "id": user.id,
            "fullName": user.full_name,
            "email": user.email,
            "role": user.role.value,
        },
        "temporaryPassword": password_value,
        "loginLink": login_link,
    }


def list_office_departments(
    db: Session,
    *,
    user_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": []}

    rows = (
        db.query(OfficeDepartment)
        .filter(OfficeDepartment.office_id == office.id)
        .order_by(OfficeDepartment.name.asc())
        .limit(limit)
        .all()
    )
    items = [_serialize_office_department(row) for row in rows]
    return {"office": _office_payload(office), "items": items}


def list_office_department_staff_counts(
    db: Session,
    *,
    user_id: str,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": []}

    rows = (
        db.query(
            OfficeDepartment.name,
            func.count(OfficeMember.id),
        )
        .outerjoin(
            OfficeMember,
            (OfficeMember.office_id == OfficeDepartment.office_id)
            & (func.lower(OfficeMember.department) == func.lower(OfficeDepartment.name)),
        )
        .filter(OfficeDepartment.office_id == office.id)
        .group_by(OfficeDepartment.name)
        .order_by(OfficeDepartment.name.asc())
        .all()
    )
    items = [{"name": name, "staffCount": count or 0} for name, count in rows]
    return {"office": _office_payload(office), "items": items}


def create_office_department(
    db: Session,
    *,
    user_id: str,
    name: str,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")

    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Department name is required")

    existing = (
        db.query(OfficeDepartment)
        .filter(OfficeDepartment.office_id == office.id)
        .filter(func.lower(OfficeDepartment.name) == clean_name.lower())
        .first()
    )
    if existing:
        return {
            "office": _office_payload(office),
            "department": _serialize_office_department(existing),
        }

    department = OfficeDepartment(
        office_id=office.id,
        name=clean_name,
    )
    db.add(department)
    db.commit()
    db.refresh(department)
    return {
        "office": _office_payload(office),
        "department": _serialize_office_department(department),
    }


def _office_call_lookup(db: Session, *, office_id: str, call_session_id: str) -> tuple[Office | None, CallSession | None, VisitorSession | None]:
    office = db.query(Office).filter(Office.id == office_id).first()
    if not office:
        return None, None, None
    call_session = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not call_session:
        return office, None, None
    session = None
    if call_session.visitor_session_id:
        session = _office_sessions_query(db, office.id).filter(VisitorSession.id == call_session.visitor_session_id).first()
    if not session and call_session.appointment_id:
        session = (
            db.query(VisitorSession)
            .filter(VisitorSession.appointment_id == call_session.appointment_id)
            .join(Home, Home.id == VisitorSession.home_id)
            .filter(Home.office_id == office.id)
            .first()
        )
    return office, call_session, session


def _find_active_office_direct_call(
    db: Session,
    *,
    office: Office,
    caller_id: str,
    receiver_id: str,
) -> CallSession | None:
    return (
        db.query(CallSession)
        .filter(
            CallSession.homeowner_id == office.administrator_user_id,
            CallSession.caller_id == caller_id,
            CallSession.receiver_id == receiver_id,
            CallSession.visitor_session_id.is_(None),
            CallSession.appointment_id.is_(None),
            CallSession.status.in_(OFFICE_QUEUE_STATUSES | OFFICE_ACTIVE_STATUSES),
        )
        .order_by(CallSession.created_at.desc())
        .first()
    )


def _create_office_direct_call_session(
    db: Session,
    *,
    office: Office,
    caller_id: str,
    receiver_id: str,
    call_type: str,
) -> CallSession:
    active = _find_active_office_direct_call(db, office=office, caller_id=caller_id, receiver_id=receiver_id)
    if active:
        return active
    row = CallSession(
        id=str(uuid.uuid4()),
        appointment_id=None,
        visitor_session_id=None,
        security_user_id=caller_id,
        caller_id=caller_id,
        receiver_id=receiver_id,
        call_type=call_type,
        room_name=f"qring-call-{uuid.uuid4()}",
        visitor_id=f"office:{office.id}:employee:{receiver_id}",
        homeowner_id=office.administrator_user_id,
        visitor_request_id=f"office:{office.id}",
        initiated_by_role="office",
        status="ringing",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def request_office_call(
    db: Session,
    *,
    user_id: str,
    visitor_session_id: str | None = None,
    call_type: str = "audio",
    has_video: bool | None = None,
    target_role: str | None = None,
    employee_id: str | None = None,
    reception_id: str | None = None,
    security_id: str | None = None,
    visitor_name: str | None = None,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")

    receiver_id = _office_call_target_user(
        db,
        office=office,
        target_role=target_role,
        employee_id=employee_id,
        reception_id=reception_id,
        security_id=security_id,
    )
    call_kind = (call_type or "").strip().lower() or ("video" if has_video else "audio")
    session = None
    if visitor_session_id:
        session = _office_sessions_query(db, office.id).filter(VisitorSession.id == visitor_session_id).first()
        if not session:
            raise ValueError("Visitor session not found")
        row = from_thread.run(
            partial(
                start_call_session,
                db,
                visitor_session_id=session.id,
                visitor_id=session.id,
                homeowner_id=session.homeowner_id,
                security_user_id=user_id,
                caller_id=user_id,
                receiver_id=receiver_id or session.homeowner_id,
                call_type=call_kind,
                visitor_name=visitor_name or session.visitor_label,
            )
        )
    elif str(target_role or "").strip().lower() == "employee":
        if not receiver_id:
            raise ValueError("employeeId is required")
        row = _create_office_direct_call_session(
            db,
            office=office,
            caller_id=user_id,
            receiver_id=receiver_id,
            call_type=call_kind,
        )
    else:
        raise ValueError("visitorSessionId is required for this call target")
    caller = db.query(User).filter(User.id == user_id).first()
    caller_name = (caller.full_name if caller else "") or "Office"
    target_user_id = receiver_id or (session.homeowner_id if session else None)
    payload_session_id = session.id if session else row.id
    room_ids = (
        _office_call_room_ids(office, session_id=session.id, call_session_id=row.id)
        if session
        else _office_direct_call_room_ids(
            office,
            call_session_id=row.id,
            caller_user_id=user_id,
            receiver_user_id=target_user_id,
        )
    )
    payload = {
        "eventId": row.id,
        "sessionId": payload_session_id,
        "callSessionId": row.id,
        "appointmentId": row.appointment_id,
        "roomName": row.room_name,
        "deliveryRoom": f"call:{row.id}",
        "status": row.status,
        "visitorId": row.visitor_id,
        "hasVideo": bool(has_video if has_video is not None else (call_type == "video")),
        "type": row.call_type,
        "role": caller.role.value if caller and caller.role else "office",
        "callerName": caller_name,
        "callerRole": "office",
        "callerOrigin": "office dashboard",
        "homeownerName": session.visitor_label if session else (visitor_name or caller_name or "Office"),
        "receiverId": target_user_id,
        "receiverRole": str(target_role or ("employee" if not session else "visitor")),
        "officeId": office.id,
        "callScope": "employee" if not session else "visitor",
        "message": f"{caller_name} is calling from the office dashboard.",
    }
    _emit_office_event(
        event_name="office.call.requested",
        office=office,
        payload=payload,
        source="office.call.request",
        rooms=room_ids,
    )
    from_thread.run(
        partial(
            emit_signaling_notification,
            event_name="call.requested",
            rooms=set(room_ids),
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="call.requested",
                idempotency_key=build_notification_idempotency_key(
                    event_type="call.requested",
                    user_id=office.administrator_user_id,
                    session_id=payload_session_id,
                    entity_id=row.id,
                    action=row.status,
                ),
                session_id=payload_session_id,
                user_id=user_id,
                source="office.call.request",
                payload=payload,
            ),
            idempotency_key=row.id,
            source="office.call.request",
        )
    )
    return {
        "callSessionId": row.id,
        "sessionId": payload_session_id,
        "visitorSessionId": session.id if session else None,
        "visitorId": session.id if session else row.id,
        "roomName": row.room_name,
        "status": row.status,
        "callType": row.call_type,
        "rtcConfig": build_webrtc_rtc_config(),
        "office": _office_payload(office),
        "visitorName": session.visitor_label if session else (visitor_name or caller_name or "Office"),
    }


def accept_office_call(db: Session, *, user_id: str, call_session_id: str) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise ValueError("Call session not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == row.visitor_session_id).first() if row.visitor_session_id else None
    is_direct_office_call = not row.visitor_session_id and not row.appointment_id
    if is_direct_office_call and user_id not in {row.caller_id, row.receiver_id}:
        raise ValueError("Call session does not belong to this office")
    if not session and not is_direct_office_call and row.visitor_request_id != f"office:{office.id}":
        raise ValueError("Call session does not belong to this office")
    mark_call_session_answered(db, call_session_id=call_session_id)
    payload = {
        "eventId": row.id,
        "sessionId": session.id if session else row.id,
        "callSessionId": row.id,
        "status": "accepted",
        "hasVideo": row.call_type == "video",
    }
    room_ids = (
        _office_call_room_ids(office, session_id=session.id, call_session_id=row.id)
        if session
        else _office_direct_call_room_ids(
            office,
            call_session_id=row.id,
            caller_user_id=row.caller_id,
            receiver_user_id=row.receiver_id,
        )
    )
    _emit_office_event(
        event_name="office.call.accepted",
        office=office,
        payload=payload,
        source="office.call.accept",
        rooms=room_ids,
    )
    from_thread.run(
        partial(
            emit_signaling_notification,
            event_name="call.accepted",
            rooms=set(room_ids),
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="call.accepted",
                idempotency_key=build_notification_idempotency_key(
                    event_type="call.accepted",
                    user_id=office.administrator_user_id,
                    session_id=session.id if session else row.id,
                    entity_id=row.id,
                    action="accepted",
                ),
                session_id=session.id if session else row.id,
                user_id=user_id,
                source="office.call.accept",
                payload=payload,
            ),
            idempotency_key=f"{row.id}:accepted",
            source="office.call.accept",
        )
    )
    return {"callSessionId": row.id, "status": "accepted"}


def reject_office_call(db: Session, *, user_id: str, call_session_id: str) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise ValueError("Call session not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == row.visitor_session_id).first() if row.visitor_session_id else None
    is_direct_office_call = not row.visitor_session_id and not row.appointment_id
    if is_direct_office_call and user_id not in {row.caller_id, row.receiver_id}:
        raise ValueError("Call session does not belong to this office")
    if not session and not is_direct_office_call and row.visitor_request_id != f"office:{office.id}":
        raise ValueError("Call session does not belong to this office")
    mark_call_session_rejected(db, call_session_id=call_session_id, reason="rejected")
    payload = {
        "eventId": row.id,
        "sessionId": session.id if session else row.id,
        "callSessionId": row.id,
        "status": "rejected",
        "hasVideo": row.call_type == "video",
    }
    room_ids = (
        _office_call_room_ids(office, session_id=session.id, call_session_id=row.id)
        if session
        else _office_direct_call_room_ids(
            office,
            call_session_id=row.id,
            caller_user_id=row.caller_id,
            receiver_user_id=row.receiver_id,
        )
    )
    _emit_office_event(
        event_name="office.call.rejected",
        office=office,
        payload=payload,
        source="office.call.reject",
        rooms=room_ids,
    )
    from_thread.run(
        partial(
            emit_signaling_notification,
            event_name="call.rejected",
            rooms=set(room_ids),
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="call.rejected",
                idempotency_key=build_notification_idempotency_key(
                    event_type="call.rejected",
                    user_id=office.administrator_user_id,
                    session_id=session.id if session else row.id,
                    entity_id=row.id,
                    action="rejected",
                ),
                session_id=session.id if session else row.id,
                user_id=user_id,
                source="office.call.reject",
                payload=payload,
            ),
            idempotency_key=f"{row.id}:rejected",
            source="office.call.reject",
        )
    )
    return {"callSessionId": row.id, "status": "rejected"}


def end_office_call(db: Session, *, user_id: str, call_session_id: str) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if not row:
        raise ValueError("Call session not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == row.visitor_session_id).first() if row.visitor_session_id else None
    is_direct_office_call = not row.visitor_session_id and not row.appointment_id
    if is_direct_office_call and user_id not in {row.caller_id, row.receiver_id}:
        raise ValueError("Call session does not belong to this office")
    if not session and not is_direct_office_call and row.visitor_request_id != f"office:{office.id}":
        raise ValueError("Call session does not belong to this office")
    ended_row = from_thread.run(partial(end_call_session, db, call_session_id=call_session_id))
    payload = {
        "eventId": row.id,
        "sessionId": session.id if session else row.id,
        "callSessionId": row.id,
        "status": ended_row.status if ended_row else row.status,
        "hasVideo": row.call_type == "video",
    }
    room_ids = (
        _office_call_room_ids(office, session_id=session.id, call_session_id=row.id)
        if session
        else _office_direct_call_room_ids(
            office,
            call_session_id=row.id,
            caller_user_id=row.caller_id,
            receiver_user_id=row.receiver_id,
        )
    )
    _emit_office_event(
        event_name="office.call.ended",
        office=office,
        payload=payload,
        source="office.call.end",
        rooms=room_ids,
    )
    from_thread.run(
        partial(
            emit_signaling_notification,
            event_name="call.ended",
            rooms=set(room_ids),
            payload=build_notification_envelope(
                notification_id=row.id,
                event_type="call.ended",
                idempotency_key=build_notification_idempotency_key(
                    event_type="call.ended",
                    user_id=office.administrator_user_id,
                    session_id=session.id if session else row.id,
                    entity_id=row.id,
                    action=row.status,
                ),
                session_id=session.id if session else row.id,
                user_id=user_id,
                source="office.call.end",
                payload=payload,
            ),
            idempotency_key=f"{row.id}:ended",
            source="office.call.end",
        )
    )
    return {"callSessionId": row.id, "status": row.status}


def _session_queue_item(session: VisitorSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "visitorName": session.visitor_label or "Visitor",
        "visitorPhone": session.visitor_phone or "",
        "company": "",
        "purpose": session.purpose or "",
        "employeeToVisit": session.assigned_staff_name or session.requested_staff_name or "",
        "requestedStaffName": session.requested_staff_name or "",
        "assignedStaffName": session.assigned_staff_name or "",
        "assignedStaffUserId": session.assigned_staff_user_id or "",
        "department": session.assigned_staff_department or "",
        "status": _status_label(session.status),
        "rawStatus": session.status,
        "time": session.started_at.isoformat() if session.started_at else utc_now().isoformat(),
        "snapshotUrl": session.snapshot_url or session.photo_url or "",
        "homeId": session.home_id,
        "doorId": session.door_id,
        "hostName": session.assigned_staff_name or session.requested_staff_name or "",
        "vehicleInfo": "",
    }


def _session_visitors_item(session: VisitorSession) -> dict[str, Any]:
    ended = session.ended_at or session.state_updated_at
    duration_seconds = None
    if ended and session.started_at:
        duration_seconds = max(0, int((ended - session.started_at).total_seconds()))
    return {
        "id": session.id,
        "visitorName": session.visitor_label or "Visitor",
        "visitorPhone": session.visitor_phone or "",
        "company": "",
        "purpose": session.purpose or "",
        "employeeToVisit": session.assigned_staff_name or session.requested_staff_name or "",
        "requestedStaffName": session.requested_staff_name or "",
        "assignedStaffName": session.assigned_staff_name or "",
        "assignedStaffUserId": session.assigned_staff_user_id or "",
        "department": session.assigned_staff_department or "",
        "status": session.status,
        "statusLabel": _status_label(session.status),
        "checkedInAt": session.homeowner_decision_at.isoformat() if session.homeowner_decision_at else None,
        "checkedOutAt": session.ended_at.isoformat() if session.ended_at else None,
        "durationSeconds": duration_seconds,
        "snapshotUrl": session.snapshot_url or session.photo_url or "",
        "homeId": session.home_id,
        "doorId": session.door_id,
        "hostName": session.assigned_staff_name or session.requested_staff_name or "",
        "vehicleInfo": "",
        "time": session.started_at.isoformat() if session.started_at else utc_now().isoformat(),
    }


def _emit_office_event(
    *,
    event_name: str,
    office: Office,
    payload: dict[str, Any],
    source: str,
    rooms: list[str] | None = None,
) -> None:
    room_list = set(rooms or _office_room_ids(office))
    room_list.add(f"user:{office.administrator_user_id}")
    receiver_id = str(payload.get("receiverId") or "").strip()
    if receiver_id:
        room_list.add(f"user:{receiver_id}")
    envelope = build_notification_envelope(
        event_type=event_name,
        payload=payload,
        session_id=payload.get("sessionId"),
        user_id=office.administrator_user_id,
        source=source,
    )
    idempotency_key = build_notification_idempotency_key(
        event_type=event_name,
        user_id=office.administrator_user_id,
        session_id=payload.get("sessionId"),
        entity_id=payload.get("sessionId"),
        action=source,
    )
    try:
        from_thread.run(
            partial(
                emit_dashboard_notification,
                event_name=event_name,
                payload=envelope,
                idempotency_key=idempotency_key,
                source=source,
                rooms=list(room_list),
            ),
        )
    except Exception:
        # Best-effort only. Queue state should still persist even if realtime is down.
        pass


def _office_payload(office: Office) -> dict[str, Any]:
    return {
        "id": office.id,
        "companyName": office.company_name,
        "businessEmail": office.business_email,
        "phoneNumber": office.phone_number,
        "officeAddress": office.office_address,
        "country": office.country,
        "state": office.state,
        "city": office.city,
        "employeeCount": office.employee_count,
        "qrId": office.qr_id,
        "scanUrl": f"/scan/{office.qr_id}",
        "receptionHomeId": office.reception_home_id,
        "receptionDoorId": office.reception_door_id,
    }


def _office_visitor_payload(
    office: Office,
    member: OfficeMember,
    *,
    call_session: CallSession,
    visitor_name: str,
    visitor_phone: str | None,
    purpose: str | None,
) -> dict[str, Any]:
    return {
        "eventId": call_session.id,
        "sessionId": call_session.id,
        "callSessionId": call_session.id,
        "appointmentId": None,
        "roomName": call_session.room_name,
        "deliveryRoom": f"call:{call_session.id}",
        "status": call_session.status,
        "visitorId": call_session.visitor_id,
        "hasVideo": call_session.call_type == "video",
        "type": call_session.call_type,
        "role": "visitor",
        "callerName": visitor_name,
        "callerRole": "visitor",
        "callerOrigin": "office visitor scan",
        "homeownerName": visitor_name,
        "visitorName": visitor_name,
        "visitorPhone": visitor_phone or "",
        "purpose": purpose or "",
        "receiverId": member.user_id,
        "receiverRole": "employee",
        "employeeId": member.user_id,
        "employeeName": member.full_name,
        "officeId": office.id,
        "officeName": office.company_name,
        "callScope": "employee",
        "message": f"{visitor_name} selected {member.full_name} at {office.company_name}.",
    }


def _base_empty_response() -> dict[str, Any]:
    return {
        "office": None,
        "metrics": {
            "liveQueue": 0,
            "pendingApprovals": 0,
            "visitorsInside": 0,
            "employeesOnline": 0,
            "todayAppointments": 0,
            "recentConversations": 0,
            "securityAlerts": 0,
            "recentDeliveries": 0,
        },
        "recentQueue": [],
        "recentActivity": [],
        "recentMessages": [],
        "securityAlerts": [],
        "quickActions": [],
        "employees": [],
    }


def _get_office_data(db: Session, user_id: str) -> tuple[Office | None, list[VisitorSession], list[OfficeMember], list[Message], list[Notification]]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return None, [], [], [], []

    sessions = _office_sessions_query(db, office.id).order_by(VisitorSession.started_at.desc()).all()
    members = (
        db.query(OfficeMember)
        .filter(OfficeMember.office_id == office.id)
        .order_by(OfficeMember.created_at.desc())
        .all()
    )
    messages = _office_messages_query(db, office.id).order_by(Message.created_at.desc()).limit(30).all()
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == office.administrator_user_id)
        .order_by(Notification.created_at.desc())
        .limit(30)
        .all()
    )
    return office, sessions, members, messages, notifications


def get_office_overview(db: Session, *, user_id: str) -> dict[str, Any]:
    office, sessions, members, messages, notifications = _get_office_data(db, user_id)
    if not office:
        return _base_empty_response()

    queue_sessions = [session for session in sessions if str(session.status or "").lower() in OFFICE_QUEUE_STATUSES]
    active_sessions = [session for session in sessions if str(session.status or "").lower() in OFFICE_ACTIVE_STATUSES]
    delivery_sessions = [session for session in sessions if str(session.visitor_type or "").lower() == "delivery"]
    alert_notifications = [item for item in notifications if "alert" in str(item.kind or "").lower()]

    recent_messages = [
        {
            "id": message.id,
            "sessionId": message.session_id,
            "from": message.sender_type,
            "text": message.body,
            "time": message.created_at.isoformat(),
            "unread": message.read_by_homeowner_at is None and message.sender_type != "homeowner",
        }
        for message in messages[:6]
    ]

    activity: list[dict[str, Any]] = []
    for session in sessions[:6]:
        activity.append(
            {
                "id": f"session-{session.id}",
                "event": session.visitor_label or "Visitor",
                "details": session.purpose or "Office visitor request",
                "time": session.started_at.isoformat() if session.started_at else utc_now().isoformat(),
                "state": session.status,
            }
        )
    for note in notifications[:6]:
        try:
            payload = json.loads(note.payload or "{}")
        except Exception:
            payload = {}
        activity.append(
            {
                "id": note.id,
                "event": str(note.kind or "notification").replace(".", " ").title(),
                "details": str(payload.get("message") or "Office activity"),
                "time": note.created_at.isoformat() if note.created_at else utc_now().isoformat(),
                "state": "notification",
            }
        )
    activity.sort(key=lambda item: item.get("time") or "", reverse=True)

    availability_counts = Counter(str(member.availability_status or "unknown").lower() for member in members)
    active_employee_count = len([member for member in members if str(member.status or "").lower() == "active"])

    return {
        "office": _office_payload(office),
        "roleCapabilities": {"office_staff": OFFICE_STAFF_CAPABILITIES},
        "metrics": {
            "liveQueue": len(queue_sessions),
            "pendingApprovals": len(queue_sessions),
            "visitorsInside": len(active_sessions),
            "employeesOnline": active_employee_count,
            "todayAppointments": 0,
            "recentConversations": len(messages),
            "securityAlerts": len(alert_notifications),
            "recentDeliveries": len(delivery_sessions),
        },
        "recentQueue": [_session_queue_item(session) for session in queue_sessions[:5]],
        "recentActivity": activity[:6],
        "recentMessages": recent_messages,
        "securityAlerts": [
            {
                "id": item.id,
                "event": str(item.kind or "alert").replace(".", " ").title(),
                "details": (json.loads(item.payload or "{}").get("message") if item.payload else "") or "Security alert",
                "time": item.created_at.isoformat() if item.created_at else utc_now().isoformat(),
            }
            for item in alert_notifications[:6]
        ],
        "quickActions": [
            {"label": "Live Queue", "to": "/dashboard/office/queue"},
            {"label": "Visitors", "to": "/dashboard/office/visitors"},
            {"label": "Messages", "to": "/dashboard/office/messages"},
            {"label": "Employees", "to": "/dashboard/office/employees"},
            {"label": "QR Entry", "to": "/dashboard/office/qr"},
            {"label": "Settings", "to": "/dashboard/office/settings"},
        ],
        "employees": [
            {
                "id": member.id,
                "name": member.full_name,
                "role": member.role_label,
                "department": member.department,
                "floor": member.floor,
                "extension": member.extension,
                "availability": member.availability_status,
                "status": member.status,
            }
            for member in members
        ],
        "availability": dict(availability_counts),
    }


def list_office_queue(
    db: Session,
    *,
    user_id: str,
    search: str | None = None,
    status: str | None = None,
    department: str | None = None,
    employee: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": [], "metrics": {"liveQueue": 0, "pendingApprovals": 0}}
    sessions = _office_sessions_query(db, office.id).order_by(VisitorSession.started_at.desc()).all()
    items = [
        _session_queue_item(session)
        for session in sessions
        if str(session.status or "").lower() in OFFICE_QUEUE_STATUSES
    ]
    term = str(search or "").strip().lower()
    if term:
        items = [
            item
            for item in items
            if term in str(item.get("visitorName") or "").lower()
            or term in str(item.get("visitorPhone") or "").lower()
            or term in str(item.get("purpose") or "").lower()
            or term in str(item.get("company") or "").lower()
        ]
    if status:
        normalized = str(status).strip().lower()
        items = [item for item in items if str(item.get("rawStatus") or "").lower() == normalized or str(item.get("status") or "").lower() == normalized]
    if employee:
        emp = str(employee).strip().lower()
        items = [item for item in items if emp in str(item.get("employeeToVisit") or "").lower()]
    if department:
        dept = str(department).strip().lower()
        items = [item for item in items if dept in str(item.get("department") or "").lower()]
    return {
        "office": _office_payload(office),
        "items": items[:limit],
        "metrics": {"liveQueue": len(items), "pendingApprovals": len(items)},
    }


def list_office_visitors(
    db: Session,
    *,
    user_id: str,
    search: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": []}
    sessions = _office_sessions_query(db, office.id).order_by(VisitorSession.started_at.desc()).all()
    items = [_session_visitors_item(session) for session in sessions]
    term = str(search or "").strip().lower()
    if term:
        items = [
            item
            for item in items
            if term in str(item.get("visitorName") or "").lower()
            or term in str(item.get("visitorPhone") or "").lower()
            or term in str(item.get("purpose") or "").lower()
        ]
    if status:
        normalized = str(status).strip().lower()
        items = [item for item in items if str(item.get("status") or "").lower() == normalized]
    return {"office": _office_payload(office), "items": items[:limit]}


def list_office_employees(
    db: Session,
    *,
    user_id: str,
    search: str | None = None,
    department: str | None = None,
    role: str | None = None,
    availability: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": []}
    members = (
        db.query(OfficeMember)
        .filter(OfficeMember.office_id == office.id)
        .order_by(OfficeMember.created_at.desc())
        .all()
    )
    items = [
        {
            "id": member.id,
            "name": member.full_name,
            "role": member.role_label,
            "department": member.department,
            "floor": member.floor,
            "extension": member.extension,
            "availability": member.availability_status,
            "status": member.status,
            "userId": member.user_id,
            "detailsSentAt": member.details_sent_at.isoformat() if member.details_sent_at else None,
        }
        for member in members
    ]
    term = str(search or "").strip().lower()
    if term:
        items = [
            item
            for item in items
            if term in str(item.get("name") or "").lower()
            or term in str(item.get("role") or "").lower()
            or term in str(item.get("department") or "").lower()
        ]
    if department:
        dept = str(department).strip().lower()
        items = [item for item in items if dept in str(item.get("department") or "").lower()]
    if role:
        role_term = str(role).strip().lower()
        items = [item for item in items if role_term in str(item.get("role") or "").lower()]
    if availability:
        avail = str(availability).strip().lower()
        items = [item for item in items if avail in str(item.get("availability") or "").lower()]
    return {"office": _office_payload(office), "items": items[:limit]}


def request_office_visitor_call(
    db: Session,
    *,
    qr_id: str,
    staff_name: str,
    visitor_name: str,
    visitor_phone: str | None = None,
    purpose: str | None = None,
    request_id: str | None = None,
    call_type: str = "audio",
    has_video: bool | None = None,
) -> dict[str, Any]:
    office = _find_office_by_qr_id(db, qr_id)
    if not office:
        raise ValueError("Office not found")
    receptionist_home_id = office.reception_home_id
    receptionist_door_id = office.reception_door_id
    if not receptionist_home_id or not receptionist_door_id:
        raise ValueError("Office reception setup is incomplete")

    requested_name = str(staff_name or "").strip()
    if not requested_name:
        raise ValueError("Staff name is required")

    caller_name = str(visitor_name or "Visitor").strip() or "Visitor"
    session = VisitorSession(
        request_id=f"office:{office.id}:{request_id or uuid.uuid4()}",
        qr_id=office.qr_id,
        home_id=receptionist_home_id,
        door_id=receptionist_door_id,
        homeowner_id=office.administrator_user_id,
        visitor_label=caller_name,
        status="pending",
        visitor_type="guest",
        request_source="office_qr",
        creator_role="visitor",
        visitor_phone=visitor_phone,
        purpose=purpose,
        requested_staff_name=requested_name,
        assigned_staff_user_id=None,
        assigned_staff_name=None,
        assigned_staff_department=None,
        started_at=utc_now(),
        state_updated_at=utc_now(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    payload = {
        "sessionId": session.id,
        "visitorName": caller_name,
        "visitorPhone": visitor_phone or "",
        "purpose": purpose or "",
        "requestedStaffName": requested_name,
        "assignedStaffName": "",
        "assignedStaffUserId": "",
        "status": session.status,
        "rawStatus": session.status,
        "officeId": office.id,
        "officeName": office.company_name,
        "homeId": session.home_id,
        "doorId": session.door_id,
        "time": session.started_at.isoformat() if session.started_at else utc_now().isoformat(),
        "message": f"{caller_name} requested {requested_name} at {office.company_name}.",
    }
    _emit_office_event(
        event_name="office.visitor_request.created",
        office=office,
        payload=payload,
        source="office.visitor.request",
        rooms=_office_room_ids(office),
    )
    create_notification(
        db=db,
        user_id=office.administrator_user_id,
        kind="office.visitor_request.created",
        payload={
            **payload,
            "route": "/dashboard/office/queue",
            "message": f"New office request for {requested_name} from {caller_name}.",
        },
    )
    return {
        "sessionId": session.id,
        "status": session.status,
        "office": _office_payload(office),
        "visitorName": caller_name,
        "visitorPhone": visitor_phone or "",
        "purpose": purpose or "",
        "requestedStaffName": requested_name,
        "assignedStaffName": "",
        "roomName": f"office-queue-{session.id}",
    }


def record_office_staff_clock(
    db: Session,
    *,
    qr_id: str,
    employee_id: str,
    staff_name: str,
    action: str,
    note: str | None = None,
) -> dict[str, Any]:
    office = _find_office_by_qr_id(db, qr_id)
    if not office:
        raise ValueError("Office not found")

    member = (
        db.query(OfficeMember)
        .filter(
            OfficeMember.office_id == office.id,
            OfficeMember.user_id == employee_id,
        )
        .first()
    )
    if not member:
        raise ValueError("Selected employee is not part of this office")

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"clock_in", "clock_out"}:
        raise ValueError("Invalid staff action")

    employee_name = str(staff_name or member.full_name or "").strip() or member.full_name
    log = OfficeAttendanceLog(
        office_id=office.id,
        office_member_id=member.id,
        user_id=member.user_id,
        employee_name=employee_name,
        action=normalized_action,
        note=(note or "").strip() or None,
        source="qr_scan",
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    payload = {
        "id": log.id,
        "attendanceId": log.id,
        "officeId": office.id,
        "officeName": office.company_name,
        "action": normalized_action,
        "status": "checked_in" if normalized_action == "clock_in" else "checked_out",
        "employee": _serialize_office_member(member),
        "employeeName": employee_name,
        "note": log.note or "",
        "recordedAt": log.created_at.isoformat() if log.created_at else utc_now().isoformat(),
        "message": f"{employee_name} recorded as {'checked in' if normalized_action == 'clock_in' else 'checked out'}.",
    }

    create_notification(
        db=db,
        user_id=office.administrator_user_id,
        kind=f"office.staff.{payload['status']}",
        payload={
            "officeId": office.id,
            "employeeId": member.user_id,
            "employeeName": employee_name,
            "status": payload["status"],
            "action": normalized_action,
            "note": log.note or "",
            "attendanceId": log.id,
            "message": payload["message"],
        },
    )
    _emit_office_event(
        event_name=f"office.staff.{payload['status']}",
        office=office,
        payload={
            "officeId": office.id,
            "employeeId": member.user_id,
            "employeeName": employee_name,
            "status": payload["status"],
            "action": normalized_action,
            "note": log.note or "",
            "attendanceId": log.id,
        },
        source="office.staff.clock",
    )

    return payload


def _attendance_log_item(log: OfficeAttendanceLog, member: OfficeMember | None) -> dict[str, Any]:
    action = str(log.action or "").strip().lower()
    status = "checked_in" if action == "clock_in" else "checked_out" if action == "clock_out" else action
    return {
        "id": log.id,
        "attendanceId": log.id,
        "officeId": log.office_id,
        "officeMemberId": log.office_member_id,
        "userId": log.user_id,
        "employeeName": log.employee_name,
        "action": action,
        "status": status,
        "note": log.note or "",
        "source": log.source,
        "recordedAt": log.created_at.isoformat() if log.created_at else utc_now().isoformat(),
        "employee": _serialize_office_member(member) if member else None,
    }


def _parse_attendance_date_bound(value: str | None, *, end: bool = False) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed_date = date.fromisoformat(normalized)
        if end:
            return datetime.combine(parsed_date + timedelta(days=1), datetime.min.time())
        return datetime.combine(parsed_date, datetime.min.time())
    except ValueError:
        parsed = datetime.fromisoformat(normalized)
        if end:
            return parsed + timedelta(days=1)
        return parsed


def _attendance_base_query(
    db: Session,
    *,
    office_id: str,
    search: str | None = None,
    action: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    query = (
        db.query(OfficeAttendanceLog, OfficeMember)
        .outerjoin(OfficeMember, OfficeMember.id == OfficeAttendanceLog.office_member_id)
        .filter(OfficeAttendanceLog.office_id == office_id)
    )

    normalized_action = str(action or "").strip().lower()
    if normalized_action in {"clock_in", "clock_out"}:
        query = query.filter(OfficeAttendanceLog.action == normalized_action)

    start_bound = _parse_attendance_date_bound(start_date)
    if start_bound is not None:
        query = query.filter(OfficeAttendanceLog.created_at >= start_bound)

    end_bound = _parse_attendance_date_bound(end_date, end=True)
    if end_bound is not None:
        query = query.filter(OfficeAttendanceLog.created_at < end_bound)

    normalized_search = str(search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        query = query.filter(
            or_(
                OfficeAttendanceLog.employee_name.ilike(pattern),
                OfficeAttendanceLog.note.ilike(pattern),
                OfficeAttendanceLog.source.ilike(pattern),
                OfficeMember.full_name.ilike(pattern),
                OfficeMember.department.ilike(pattern),
                OfficeMember.role_label.ilike(pattern),
                OfficeMember.extension.ilike(pattern),
            )
        )

    return query


def list_office_attendance(
    db: Session,
    *,
    user_id: str,
    search: str | None = None,
    action: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {
            "office": None,
            "items": [],
            "metrics": {"total": 0, "clockIns": 0, "clockOuts": 0, "uniqueEmployees": 0},
            "pagination": {"page": 1, "limit": max(1, min(limit, 100)), "total": 0, "hasMore": False, "nextPage": None},
        }

    page_size = max(1, min(int(limit or 50), 100))
    current_page = max(1, int(page or 1))
    offset = (current_page - 1) * page_size

    base_query = _attendance_base_query(
        db,
        office_id=office.id,
        search=search,
        action=action,
        start_date=start_date,
        end_date=end_date,
    )

    total = _attendance_base_query(
        db,
        office_id=office.id,
        search=search,
        action=action,
        start_date=start_date,
        end_date=end_date,
    ).with_entities(func.count(OfficeAttendanceLog.id)).scalar() or 0
    clock_ins = (
        _attendance_base_query(
            db,
            office_id=office.id,
            search=search,
            action="clock_in",
            start_date=start_date,
            end_date=end_date,
        )
        .with_entities(func.count(OfficeAttendanceLog.id))
        .scalar()
        or 0
    )
    clock_outs = (
        _attendance_base_query(
            db,
            office_id=office.id,
            search=search,
            action="clock_out",
            start_date=start_date,
            end_date=end_date,
        )
        .with_entities(func.count(OfficeAttendanceLog.id))
        .scalar()
        or 0
    )
    unique_employees = (
        base_query.with_entities(func.count(func.distinct(func.coalesce(OfficeAttendanceLog.user_id, OfficeAttendanceLog.employee_name))))
        .scalar()
        or 0
    )

    rows = (
        base_query.order_by(OfficeAttendanceLog.created_at.desc()).offset(offset).limit(page_size + 1).all()
    )
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    items = [_attendance_log_item(log, member) for log, member in rows]
    return {
        "office": _office_payload(office),
        "items": items,
        "metrics": {
            "total": int(total),
            "clockIns": clock_ins,
            "clockOuts": clock_outs,
            "uniqueEmployees": unique_employees,
        },
        "pagination": {
            "page": current_page,
            "limit": page_size,
            "total": int(total),
            "hasMore": has_more,
            "nextPage": current_page + 1 if has_more else None,
        },
    }


def export_office_attendance_csv(
    db: Session,
    *,
    user_id: str,
    search: str | None = None,
    action: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[str, str]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")

    rows = (
        _attendance_base_query(
            db,
            office_id=office.id,
            search=search,
            action=action,
            start_date=start_date,
            end_date=end_date,
        )
        .order_by(OfficeAttendanceLog.created_at.desc())
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "recordedAt",
        "employeeName",
        "action",
        "status",
        "department",
        "role",
        "floor",
        "extension",
        "note",
        "source",
        "officeName",
    ])
    for log, member in rows:
        writer.writerow([
            log.created_at.isoformat() if log.created_at else "",
            log.employee_name,
            log.action,
            "checked_in" if log.action == "clock_in" else "checked_out" if log.action == "clock_out" else log.action,
            member.department if member else "",
            member.role_label if member else "",
            member.floor if member else "",
            member.extension if member else "",
            log.note or "",
            log.source,
            office.company_name,
        ])

    filename = f"{office.company_name or 'office'}-attendance.csv".replace(" ", "-").lower()
    return buffer.getvalue(), filename


def get_office_visitor_call_status(db: Session, *, call_session_id: str) -> dict[str, Any]:
    row = db.query(CallSession).filter(CallSession.id == call_session_id).first()
    if row and str(row.visitor_request_id or "").startswith("office:"):
        visitor_request_parts = str(row.visitor_request_id or "").split(":")
        office_id = visitor_request_parts[1] if len(visitor_request_parts) > 1 else ""
        office = db.query(Office).filter(Office.id == office_id).first() if office_id else None
        member = db.query(OfficeMember).filter(OfficeMember.user_id == row.receiver_id).first() if row.receiver_id else None

        return {
            "callSessionId": row.id,
            "status": row.status,
            "callType": row.call_type,
            "office": _office_payload(office) if office else None,
            "employee": _serialize_office_member(member) if member else None,
            "visitorName": str(row.visitor_id or "").split(":")[-1] if row.visitor_id else None,
            "roomName": row.room_name,
            "receiverId": row.receiver_id,
            "startedAt": row.created_at.isoformat() if row.created_at else None,
            "endedAt": row.ended_at.isoformat() if row.ended_at else None,
        }

    session = db.query(VisitorSession).filter(VisitorSession.id == call_session_id).first()
    if session and str(session.request_id or "").startswith("office:"):
        office_id = str(session.request_id or "").split(":")[1] if ":" in str(session.request_id or "") else ""
        office = db.query(Office).filter(Office.id == office_id).first() if office_id else None
        return {
            "callSessionId": session.id,
            "sessionId": session.id,
            "status": session.status,
            "callType": session.request_source or "office",
            "office": _office_payload(office) if office else None,
            "employee": None,
            "visitorName": session.visitor_label or "Visitor",
            "requestedStaffName": session.requested_staff_name or "",
            "assignedStaffName": session.assigned_staff_name or "",
            "assignedStaffUserId": session.assigned_staff_user_id or "",
            "roomName": f"office-queue-{session.id}",
            "receiverId": session.assigned_staff_user_id or None,
            "startedAt": session.started_at.isoformat() if session.started_at else None,
            "endedAt": session.ended_at.isoformat() if session.ended_at else None,
        }

    raise ValueError("Call session not found")


def list_office_conversations(
    db: Session,
    *,
    user_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "items": []}
    members = (
        db.query(OfficeMember)
        .filter(OfficeMember.office_id == office.id, OfficeMember.user_id.isnot(None))
        .order_by(OfficeMember.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for member in members:
        conversation = _get_office_staff_conversation(db, office_id=office.id, staff_user_id=member.user_id)
        items.append(_staff_conversation_item(db, office=office, conversation=conversation, current_user_id=user_id))
    items.sort(key=lambda item: item.get("time") or "", reverse=True)
    return {"office": _office_payload(office), "items": items}


def list_office_conversation_messages(
    db: Session,
    *,
    user_id: str,
    session_id: str,
    limit: int = 300,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        return {"office": None, "conversation": None, "items": []}
    conversation = (
        db.query(OfficeStaffConversation)
        .filter(OfficeStaffConversation.office_id == office.id)
        .filter(OfficeStaffConversation.id == session_id)
        .first()
    )
    if not conversation:
        member = db.query(OfficeMember).filter(OfficeMember.office_id == office.id, OfficeMember.user_id == session_id).first()
        if member:
            conversation = _get_office_staff_conversation(db, office_id=office.id, staff_user_id=member.user_id)
    if not conversation:
        return {"office": _office_payload(office), "conversation": None, "items": []}
    rows = (
        db.query(OfficeStaffMessage)
        .filter(OfficeStaffMessage.conversation_id == conversation.id)
        .order_by(OfficeStaffMessage.created_at.asc())
        .limit(limit)
        .all()
    )
    items = [
        {
            "id": row.id,
            "sessionId": conversation.id,
            "text": row.body,
            "senderType": row.sender_role,
            "displayName": "Office" if row.sender_role == "office" else "Staff",
            "time": row.created_at.isoformat(),
            "read": row.sender_role == "office" or row.read_at is not None,
        }
        for row in rows
    ]
    return {
        "office": _office_payload(office),
        "conversation": {
            "id": conversation.id,
            "staffUserId": conversation.staff_user_id,
            "staffName": (
                db.query(OfficeMember.full_name)
                .filter(OfficeMember.office_id == office.id, OfficeMember.user_id == conversation.staff_user_id)
                .scalar()
            )
            or (
                db.query(User.full_name)
                .filter(User.id == conversation.staff_user_id)
                .scalar()
            )
            or "Staff",
            "status": "active",
        },
        "items": items,
    }


def create_office_message(
    db: Session,
    *,
    user_id: str,
    session_id: str,
    text: str,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    body = (text or "").strip()
    if not body:
        raise ValueError("Message text is required")
    conversation = (
        db.query(OfficeStaffConversation)
        .filter(OfficeStaffConversation.office_id == office.id)
        .filter(OfficeStaffConversation.id == session_id)
        .first()
    )
    if not conversation:
        member = db.query(OfficeMember).filter(OfficeMember.office_id == office.id, OfficeMember.user_id == session_id).first()
        if member:
            conversation = _get_office_staff_conversation(db, office_id=office.id, staff_user_id=member.user_id)
    if not conversation:
        raise ValueError("Conversation not found")
    message = OfficeStaffMessage(
        conversation_id=conversation.id,
        office_id=office.id,
        staff_user_id=conversation.staff_user_id,
        sender_user_id=user_id,
        sender_role="office" if user_id != conversation.staff_user_id else "staff",
        body=body,
        created_at=utc_now(),
    )
    conversation.last_message_at = utc_now()
    db.add(message)
    db.commit()
    db.refresh(message)
    db.refresh(conversation)
    staff_member = (
        db.query(OfficeMember)
        .filter(OfficeMember.office_id == office.id, OfficeMember.user_id == conversation.staff_user_id)
        .first()
    )
    create_notification(
        db=db,
        user_id=conversation.staff_user_id,
        kind="office.message.created",
        payload={
            "sessionId": conversation.id,
            "messageId": message.id,
            "text": message.body,
            "officeId": office.id,
            "staffUserId": conversation.staff_user_id,
            "staffName": staff_member.full_name if staff_member else "Staff",
            "message": f"New office message for {staff_member.full_name if staff_member else 'staff'}.",
        },
    )
    _emit_office_event(
        event_name="office.message.created",
        office=office,
        payload={
            "sessionId": conversation.id,
            "messageId": message.id,
            "text": message.body,
            "officeId": office.id,
            "staffUserId": conversation.staff_user_id,
        },
        source="office.message.create",
        rooms=_office_direct_call_room_ids(office, receiver_user_id=conversation.staff_user_id),
    )
    return {
        "id": message.id,
        "sessionId": conversation.id,
        "text": message.body,
        "senderType": message.sender_role,
        "displayName": "Office",
        "time": message.created_at.isoformat(),
        "read": True,
    }


def update_office_visitor_status(
    db: Session,
    *,
    user_id: str,
    session_id: str,
    status: str,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == session_id).first()
    if not session:
        raise ValueError("Visitor request not found")
    normalized = str(status or "").strip().lower()
    session.status = normalized
    session.state_updated_at = utc_now()
    if normalized in {"approved", "checked_in"}:
        session.homeowner_decision_at = session.homeowner_decision_at or utc_now()
        session.gate_status = "waiting"
    if normalized in {"rejected", "cancelled", "expired", "checked_out"}:
        session.ended_at = session.ended_at or utc_now()
        session.gate_status = "denied_at_gate"
    db.commit()
    db.refresh(session)
    create_notification(
        db=db,
        user_id=session.homeowner_id,
        kind=f"office.visitor_request.{normalized}",
        payload={
            "sessionId": session.id,
            "officeId": office.id,
            "status": normalized,
            "visitorName": session.visitor_label or "Visitor",
            "message": f"Visitor request {normalized}.",
        },
    )
    _emit_office_event(
        event_name=f"office.visitor_request.{normalized}",
        office=office,
        payload={
            "sessionId": session.id,
            "officeId": office.id,
            "status": normalized,
            "visitorName": session.visitor_label or "Visitor",
            "snapshotUrl": session.snapshot_url or session.photo_url or "",
        },
        source=f"office.visitor_request.{normalized}",
    )
    return _session_visitors_item(session)


def assign_office_visitor_request(
    db: Session,
    *,
    user_id: str,
    session_id: str,
    assignee_user_id: str | None = None,
    assignee_name: str | None = None,
    assignee_department: str | None = None,
) -> dict[str, Any]:
    office = _find_office_for_user(db, user_id)
    if not office:
        raise ValueError("Office not found")
    session = _office_sessions_query(db, office.id).filter(VisitorSession.id == session_id).first()
    if not session:
        raise ValueError("Visitor request not found")
    member = None
    normalized_assignee_user_id = str(assignee_user_id or "").strip()
    if normalized_assignee_user_id:
        member = (
            db.query(OfficeMember)
            .filter(
                OfficeMember.office_id == office.id,
                OfficeMember.user_id == normalized_assignee_user_id,
            )
            .first()
        )
    if not member:
        member = _resolve_office_member_by_name(
            db,
            office_id=office.id,
            name=assignee_name or session.assigned_staff_name or session.requested_staff_name,
            department=assignee_department or session.assigned_staff_department,
        )
    if not member:
        raise ValueError("Selected staff member is not part of this office")
    if not member.user_id:
        raise ValueError("Selected staff member does not have an account")

    session.assigned_staff_user_id = member.user_id
    session.assigned_staff_name = member.full_name
    session.assigned_staff_department = member.department
    session.status = "assigned_to_staff"
    session.state_updated_at = utc_now()
    db.commit()
    db.refresh(session)
    staff_conversation = _get_office_staff_conversation(db, office_id=office.id, staff_user_id=member.user_id)
    create_office_message(
        db=db,
        user_id=user_id,
        session_id=staff_conversation.id,
        text=f"Visitor request assigned: {session.visitor_label or 'Visitor'} for {session.requested_staff_name or member.full_name}.",
    )
    create_notification(
        db=db,
        user_id=member.user_id,
        kind="office.visitor_request.assigned",
        payload={
            "sessionId": session.id,
            "officeId": office.id,
            "assigneeName": member.full_name,
            "department": member.department or "",
            "staffUserId": member.user_id,
            "message": f"Visitor request assigned to {member.full_name}.",
        },
    )
    _emit_office_event(
        event_name="office.visitor_request.assigned",
        office=office,
        payload={
            "sessionId": session.id,
            "officeId": office.id,
            "assigneeName": member.full_name,
            "department": member.department or "",
            "staffUserId": member.user_id,
            "status": session.status,
            "requestedStaffName": session.requested_staff_name or "",
        },
        source="office.visitor_request.assign",
        rooms=_office_direct_call_room_ids(office, receiver_user_id=member.user_id),
    )
    return _session_visitors_item(session)
