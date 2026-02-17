import uuid
import json
import base64
import logging
from threading import Lock
from datetime import datetime

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

settings = get_settings()
_firebase_init_lock = Lock()
logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth
except ImportError:
    firebase_admin = None
    firebase_auth = None


def _ensure_firebase_app():
    if firebase_admin is None or firebase_auth is None:
        raise AppException(
            "Firebase Admin SDK is not installed. Add firebase-admin to dependencies.",
            status_code=500,
        )

    if firebase_admin._apps:
        return firebase_admin.get_app()

    with _firebase_init_lock:
        if firebase_admin._apps:
            return firebase_admin.get_app()
        if not settings.FIREBASE_PROJECT_ID:
            raise AppException("FIREBASE_PROJECT_ID is not configured", status_code=500)
        return firebase_admin.initialize_app(options={"projectId": settings.FIREBASE_PROJECT_ID})


def _verify_google_id_token(id_token: str, expected_email: str | None = None) -> tuple[str, str]:
    if not id_token:
        raise AppException("idToken is required", status_code=400)

    app = _ensure_firebase_app()
    try:
        decoded = firebase_auth.verify_id_token(id_token, app=app)
    except Exception as exc:
        token_preview = _peek_token_claims(id_token)
        logger.warning(
            "Google token verification failed: %s | project=%s | preview=%s",
            exc.__class__.__name__,
            settings.FIREBASE_PROJECT_ID,
            token_preview,
        )
        raise AppException("Invalid Google ID token", status_code=401) from exc

    email = (decoded.get("email") or "").strip().lower()
    if not email:
        raise AppException("Google account email is missing", status_code=400)
    if decoded.get("email_verified") is False:
        raise AppException("Google email is not verified", status_code=401)

    name = (decoded.get("name") or "").strip()
    return email, name


def _peek_token_claims(id_token: str) -> dict:
    """Best-effort JWT payload preview for debugging verification mismatches."""
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return {"error": "malformed_jwt"}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        claims = json.loads(decoded)
        return {
            "aud": claims.get("aud"),
            "iss": claims.get("iss"),
            "sub": claims.get("sub"),
            "email": claims.get("email"),
            "email_verified": claims.get("email_verified"),
            "auth_time": claims.get("auth_time"),
            "exp": claims.get("exp"),
            "iat": claims.get("iat"),
        }
    except Exception as exc:
        return {"error": f"peek_failed:{exc.__class__.__name__}"}


def _issue_auth_tokens(db: Session, user: User, user_agent: str = "", ip_address: str = "") -> AuthResponse:
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
                User.email.like(f"{login_key}@%"),
            )
        )
        .first()
    )
    if not user or not verify_password(password, user.password_hash):
        raise AppException("Invalid credentials", status_code=401)
    return _issue_auth_tokens(db=db, user=user, user_agent=user_agent, ip_address=ip_address)


def google_signin(
    db: Session,
    id_token: str,
    email: str | None = None,
    display_name: str | None = None,
    user_agent: str = "",
    ip_address: str = "",
) -> AuthResponse:
    token_email, _ = _verify_google_id_token(id_token=id_token, expected_email=email)
    user = db.query(User).filter(User.email == token_email).first()
    if not user:
        raise AppException("Account not found. Please sign up first.", status_code=404)
    return _issue_auth_tokens(db=db, user=user, user_agent=user_agent, ip_address=ip_address)


def google_signup(
    db: Session,
    id_token: str,
    role: str = "homeowner",
    email: str | None = None,
    display_name: str | None = None,
    user_agent: str = "",
    ip_address: str = "",
) -> AuthResponse:
    token_email, token_name = _verify_google_id_token(id_token=id_token, expected_email=email)
    existing = db.query(User).filter(User.email == token_email).first()
    if existing:
        raise AppException("Email already exists", status_code=409)

    try:
        user_role = UserRole(role)
    except ValueError as exc:
        raise AppException("Invalid role", status_code=400) from exc

    resolved_name = (display_name or token_name or token_email.split("@")[0]).strip()
    user = User(
        full_name=resolved_name,
        email=token_email,
        password_hash=hash_password(str(uuid.uuid4())),
        role=user_role,
        email_verified=True,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _issue_auth_tokens(db=db, user=user, user_agent=user_agent, ip_address=ip_address)


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
    if not user:
        raise AppException("Email not found", status_code=404)
    return {"email": email, "status": "email_verified"}


def reset_password(db: Session, email: str, new_password: str):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise AppException("Email not found", status_code=404)
    user.password_hash = hash_password(new_password)
    db.commit()
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
