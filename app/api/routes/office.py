from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.services.office_service import (
    assign_office_visitor_request,
    accept_office_call,
    create_office_message,
    end_office_call,
    get_office_overview,
    list_office_conversation_messages,
    list_office_conversations,
    list_office_employees,
    list_office_queue,
    list_office_visitors,
    reject_office_call,
    request_office_call,
    update_office_visitor_status,
)

router = APIRouter()


class OfficeCallRequestPayload(BaseModel):
    visitorSessionId: str | None = None
    type: str | None = None
    hasVideo: bool | None = None
    targetRole: str | None = None
    employeeId: str | None = None
    receptionId: str | None = None
    securityId: str | None = None
    visitorName: str | None = None


@router.get("/overview")
def office_overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": get_office_overview(db, user_id=user.id)}


@router.get("/queue")
def office_queue(
    search: str | None = None,
    status: str | None = None,
    department: str | None = None,
    employee: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {
        "data": list_office_queue(
            db,
            user_id=user.id,
            search=search,
            status=status,
            department=department,
            employee=employee,
            limit=limit,
        )
    }


@router.get("/visitors")
def office_visitors(
    search: str | None = None,
    status: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": list_office_visitors(db, user_id=user.id, search=search, status=status, limit=limit)}


@router.get("/employees")
def office_employees(
    search: str | None = None,
    department: str | None = None,
    role: str | None = None,
    availability: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {
        "data": list_office_employees(
            db,
            user_id=user.id,
            search=search,
            department=department,
            role=role,
            availability=availability,
            limit=limit,
        )
    }


@router.get("/conversations")
def office_conversations(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": list_office_conversations(db, user_id=user.id, limit=limit)}


@router.get("/conversations/{session_id}/messages")
def office_conversation_messages(
    session_id: str,
    limit: int = 300,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {
        "data": list_office_conversation_messages(
            db,
            user_id=user.id,
            session_id=session_id,
            limit=limit,
        )
    }


@router.post("/conversations/{session_id}/messages")
def office_send_message(
    session_id: str,
    text: str = Body(embed=True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return {"data": create_office_message(db, user_id=user.id, session_id=session_id, text=text)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/visitor-requests/{session_id}/approve")
def office_approve_visitor(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return {"data": update_office_visitor_status(db, user_id=user.id, session_id=session_id, status="approved")}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/visitor-requests/{session_id}/reject")
def office_reject_visitor(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return {"data": update_office_visitor_status(db, user_id=user.id, session_id=session_id, status="rejected")}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/visitor-requests/{session_id}/assign")
def office_assign_visitor(
    session_id: str,
    assignee_name: str | None = Body(default=None, embed=True),
    assignee_department: str | None = Body(default=None, embed=True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return {
            "data": assign_office_visitor_request(
                db,
                user_id=user.id,
                session_id=session_id,
                assignee_name=assignee_name,
                assignee_department=assignee_department,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/calls/request")
def office_request_call(
    payload: OfficeCallRequestPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
            return {
                "data": request_office_call(
                    db,
                    user_id=user.id,
                    visitor_session_id=payload.visitorSessionId,
                    call_type=payload.type or "audio",
                    has_video=payload.hasVideo,
                    target_role=payload.targetRole,
                employee_id=payload.employeeId,
                reception_id=payload.receptionId,
                security_id=payload.securityId,
                visitor_name=payload.visitorName,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/calls/{call_session_id}/accept")
def office_accept_call(
    call_session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return {"data": accept_office_call(db, user_id=user.id, call_session_id=call_session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/calls/{call_session_id}/reject")
def office_reject_call(
    call_session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return {"data": reject_office_call(db, user_id=user.id, call_session_id=call_session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/calls/{call_session_id}/end")
def office_end_call(
    call_session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return {"data": end_office_call(db, user_id=user.id, call_session_id=call_session_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
