import random
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from app.db.models import DeviceSession, Notification, User, UserRole
from app.schemas.auth import AuthResponse

_reset_otp_store: dict[str, dict] = {}
settings = get_settings()


def signup(
    db: Session,
    full_name: str,
    email: str,
    password: str,
    role: str,
):
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise AppException("Email already exists", status_code=409)

    try:
        user_role = UserRole(role)
    except ValueError as exc:
        raise AppException("Invalid role", status_code=400) from exc
    if user_role == UserRole.admin:
        raise AppException("Admin signup is not allowed on this endpoint", status_code=403)

    user = User(
        full_name=full_name,
        email=email,
        password_hash=hash_password(password),
        role=user_role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "email": user.email}


def admin_signup(
    db: Session,
    full_name: str,
    email: str,
    password: str,
):
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise AppException("Email already exists", status_code=409)

    user = User(
        full_name=full_name,
        email=email,
        password_hash=hash_password(password),
        role=UserRole.admin,
        email_verified=True,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "email": user.email}


def login(db: Session, email: str, password: str, user_agent: str = "", ip_address: str = "") -> AuthResponse:
    login_key = (email or "").strip().lower()
    user = (
        db.query(User)
        .filter(
            or_(
                User.email == login_key,
                User.email.like(f"{login_key}@estate.useqring.online"),
            )
        )
        .first()
    )
    if not user or not verify_password(password, user.password_hash):
        raise AppException("Invalid credentials", status_code=401)

    access_token = create_access_token(user.id, user.role.value)
    refresh_token = create_refresh_token(user.id)

    device_session = DeviceSession(
        user_id=user.id,
        refresh_token=refresh_token,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    db.add(device_session)
    existing_notification = db.query(Notification).filter(Notification.user_id == user.id).first()
    if not existing_notification:
        db.add(
            Notification(
                user_id=user.id,
                kind="system",
                payload='{"message":"Welcome to Qring dashboard. Notifications are now active."}',
            )
        )
    db.commit()

    return AuthResponse(
        accessToken=access_token,
        refreshToken=refresh_token,
        user={
            "id": user.id,
            "fullName": user.full_name,
            "email": user.email,
            "role": user.role.value,
        },
    )


def rotate_refresh_token(db: Session, refresh_token: str):
    session = (
        db.query(DeviceSession)
        .filter(DeviceSession.refresh_token == refresh_token, DeviceSession.revoked_at.is_(None))
        .first()
    )
    if not session:
        raise AppException("Invalid refresh token", status_code=401)

    access_token = create_access_token(session.user_id, session.user.role.value)
    new_refresh = create_refresh_token(session.user_id)
    session.revoked_at = datetime.utcnow()
    db.add(
        DeviceSession(
            user_id=session.user_id,
            refresh_token=new_refresh,
            user_agent=session.user_agent,
            ip_address=session.ip_address,
        )
    )
    db.commit()
    return {"accessToken": access_token, "refreshToken": new_refresh}


def logout(db: Session, refresh_token: str):
    session = db.query(DeviceSession).filter(DeviceSession.refresh_token == refresh_token).first()
    if session:
        session.revoked_at = datetime.utcnow()
        db.commit()


def request_password_reset(db: Session, email: str):
    user = db.query(User).filter(User.email == email).first()
    # Keep response stable even when user doesn't exist to avoid enumeration.
    if not user:
        return {"email": email, "status": "otp_sent"}

    otp = f"{random.randint(0, 999999):06d}"
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    _reset_otp_store[email.lower()] = {
        "otp": otp,
        "expires_at": expires_at,
    }

    return {
        "email": email,
        "status": "otp_sent",
        "otp": otp,  # dev-mode return; replace with email delivery in production
        "expiresAt": expires_at.isoformat(),
    }


def verify_password_reset_otp(email: str, otp: str):
    key = email.lower()
    entry = _reset_otp_store.get(key)
    if not entry:
        raise AppException("OTP not requested", status_code=400)
    if datetime.utcnow() > entry["expires_at"]:
        _reset_otp_store.pop(key, None)
        raise AppException("OTP expired", status_code=400)
    if entry["otp"] != otp:
        raise AppException("Invalid OTP", status_code=400)
    return {"email": email, "status": "verified"}


def reset_password(db: Session, email: str, otp: str, new_password: str):
    verify_password_reset_otp(email, otp)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise AppException("User not found", status_code=404)
    user.password_hash = hash_password(new_password)
    db.commit()
    _reset_otp_store.pop(email.lower(), None)
    return {"status": "password_reset"}


def change_password(db: Session, user_id: str, current_password: str, new_password: str):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise AppException("User not found", status_code=404)
    if not verify_password(current_password, user.password_hash):
        raise AppException("Current password is incorrect", status_code=400)
    user.password_hash = hash_password(new_password)
    db.commit()
    return {"status": "password_changed"}
