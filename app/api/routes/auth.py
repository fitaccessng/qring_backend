from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas.auth import (
    AdminSignupRequest,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    GoogleSigninRequest,
    GoogleSignupRequest,
    LoginRequest,
    LogoutRequest,
    ResetPasswordRequest,
    RefreshTokenRequest,
    SignupRequest,
)
from app.services import auth_service

router = APIRouter()


@router.post("/signup")
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    data = auth_service.signup(
        db=db,
        full_name=payload.fullName,
        email=payload.email,
        password=payload.password,
        role=payload.role,
    )
    return {"data": data}


@router.post("/admin-signup")
def admin_signup(payload: AdminSignupRequest, db: Session = Depends(get_db)):
    data = auth_service.admin_signup(
        db=db,
        full_name=payload.fullName,
        email=payload.email,
        password=payload.password,
    )
    return {"data": data}


@router.post("/login")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    data = auth_service.login(
        db=db,
        email=payload.email,
        password=payload.password,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    return {"data": data.model_dump()}


@router.post("/google-signin")
def google_signin(payload: GoogleSigninRequest, request: Request, db: Session = Depends(get_db)):
    data = auth_service.google_signin(
        db=db,
        id_token=payload.idToken,
        email=payload.email,
        display_name=payload.displayName,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    return {"data": data.model_dump()}


@router.post("/google-signup")
def google_signup(payload: GoogleSignupRequest, request: Request, db: Session = Depends(get_db)):
    data = auth_service.google_signup(
        db=db,
        id_token=payload.idToken,
        email=payload.email,
        display_name=payload.displayName,
        role=payload.role,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    return {"data": data.model_dump()}


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    return {"data": auth_service.request_password_reset(db, payload.email)}


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    return {"data": auth_service.reset_password(db, payload.email, payload.newPassword)}


@router.post("/refresh-token")
def refresh_token(payload: RefreshTokenRequest, db: Session = Depends(get_db)):
    data = auth_service.rotate_refresh_token(db, payload.refreshToken)
    return {"data": data}


@router.post("/logout")
def logout(payload: LogoutRequest, db: Session = Depends(get_db)):
    auth_service.logout(db, payload.refreshToken)
    return {"data": {"status": "ok"}}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return {"data": auth_service.change_password(db, user.id, payload.currentPassword, payload.newPassword)}
