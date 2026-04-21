from __future__ import annotations

import uuid
import json
import base64
import logging
import re
import secrets
from urllib.parse import quote
from threading import Lock
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.models import DeviceSession, Notification, User, UserRole
from app.schemas.auth import AuthResponse
from app.services.provider_integrations import send_email_smtp
from app.db.models.user_token import UserToken, UserTokenType, generate_user_token, hash_user_token

settings = get_settings()
_firebase_init_lock = Lock()
logger = logging.getLogger(__name__)
_auth_lock = Lock()
_failed_login_hits: dict[str, list[float]] = {}
_failed_login_blocked_until: dict[str, float] = {}

# 5 failures in 10 minutes => 10 minute lock.
_LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60
_LOGIN_MAX_FAILURES = 5
_LOGIN_LOCK_SECONDS = 10 * 60

_PASSWORD_MIN_LEN = 8
_TOKEN_ISSUE_WINDOW_SECONDS = 60 * 60
_TOKEN_ISSUE_MAX = 5
_token_issue_hits: dict[str, list[float]] = {}
_email_verify_attempt_hits: dict[str, list[float]] = {}

try:
    import firebase_admin
    from firebase_admin import credentials as firebase_credentials
    from firebase_admin import auth as firebase_auth
except ImportError:
    firebase_admin = None
    firebase_credentials = None
    firebase_auth = None


def _ensure_firebase_app():
    if firebase_admin is None or firebase_auth is None or firebase_credentials is None:
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
        service_account = _load_firebase_service_account()
        if service_account:
            cred = firebase_credentials.Certificate(service_account)
            return firebase_admin.initialize_app(
                credential=cred,
                options={"projectId": settings.FIREBASE_PROJECT_ID},
            )
        logger.warning(
            "Firebase service account credentials not configured. Falling back to default credentials lookup."
        )
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


def _load_firebase_service_account() -> dict | None:
    raw_json = (settings.FIREBASE_SERVICE_ACCOUNT_JSON or "").strip()
    if raw_json:
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise AppException("FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON", status_code=500) from exc

    raw_base64 = (settings.FIREBASE_SERVICE_ACCOUNT_BASE64 or "").strip()
    if raw_base64:
        try:
            decoded = base64.b64decode(raw_base64).decode("utf-8")
            return json.loads(decoded)
        except Exception as exc:
            raise AppException("FIREBASE_SERVICE_ACCOUNT_BASE64 is invalid", status_code=500) from exc

    return None


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
    db.commit()

    return AuthResponse(
        accessToken=access_token,
        refreshToken=refresh_token,
        user={
            "id": user.id,
            "fullName": user.full_name,
            "email": user.email,
            "role": user.role.value,
            "referralCode": user.referral_code,
            "referralEarnings": int(user.referral_earnings or 0),
        },
    )


def _normalize_referral_code(referral_code: str | None) -> str | None:
    if referral_code is None:
        return None
    cleaned = referral_code.strip().upper()
    return cleaned or None


def _resolve_referrer(db: Session, referral_code: str | None) -> User | None:
    code = _normalize_referral_code(referral_code)
    if not code:
        return None
    referrer = db.query(User).filter(User.referral_code == code).first()
    if not referrer:
        raise AppException("Invalid referral code", status_code=400)
    return referrer


def signup(
    db: Session,
    full_name: str,
    email: str,
    password: str,
    role: str,
    referral_code: str | None = None,
):
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise AppException("Email already exists", status_code=409)

    _validate_password_strength(password)

    normalized_role = (role or "").strip().lower()
    if normalized_role == "resident":
        normalized_role = "homeowner"

    try:
        user_role = UserRole(normalized_role)
    except ValueError as exc:
        raise AppException("Invalid role", status_code=400) from exc
    if user_role == UserRole.admin:
        raise AppException("Admin signup is not allowed on this endpoint", status_code=403)

    referrer = _resolve_referrer(db, referral_code)

    user = User(
        full_name=full_name,
        email=email,
        password_hash=hash_password(password),
        role=user_role,
        referred_by_user_id=referrer.id if referrer else None,
        email_verified=settings.ENVIRONMENT == "development",  # Auto-verify in development
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    # Send email verification (best-effort). Do not block signup on SMTP.
    if not user.email_verified:
        try:
            request_email_verification(db, user.email)
        except Exception:
            logger.exception("Unable to send verification email for user=%s", user.id)
    return {"id": user.id, "email": user.email}


def admin_signup(
    db: Session,
    full_name: str,
    email: str,
    password: str,
    admin_key: str | None = None,
):
    # Do not allow admin signup unless an explicit key is configured.
    # This endpoint is a high-risk footgun.
    configured_key = (settings.ADMIN_SIGNUP_KEY or "").strip()
    env = (settings.ENVIRONMENT or "").strip().lower()
    if env in {"production", "staging"}:
        raise AppException("Admin signup is disabled", status_code=403)
    if configured_key:
        provided = (admin_key or "").strip()
        if not provided or provided != configured_key:
            raise AppException("Invalid admin signup key", status_code=403)

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise AppException("Email already exists", status_code=409)

    _validate_password_strength(password)

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
    _enforce_login_rate_limit(login_key=login_key, ip_address=ip_address)
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
    if not user or not user.is_active:
        _record_login_failure(login_key=login_key, ip_address=ip_address)
        raise AppException("Invalid credentials", status_code=401)
    if not user.email_verified:
        # Do not issue sessions to unverified users. Allow them to request verification.
        raise AppException("Email is not verified", status_code=403)
    if not verify_password(password, user.password_hash):
        _record_login_failure(login_key=login_key, ip_address=ip_address)
        raise AppException("Invalid credentials", status_code=401)
    _clear_login_failures(login_key=login_key, ip_address=ip_address)
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
    if not user.email_verified:
        user.email_verified = True
        db.commit()
    return _issue_auth_tokens(db=db, user=user, user_agent=user_agent, ip_address=ip_address)


def google_signup(
    db: Session,
    id_token: str,
    role: str = "homeowner",
    email: str | None = None,
    display_name: str | None = None,
    referral_code: str | None = None,
    user_agent: str = "",
    ip_address: str = "",
) -> AuthResponse:
    token_email, token_name = _verify_google_id_token(id_token=id_token, expected_email=email)
    existing = db.query(User).filter(User.email == token_email).first()
    if existing:
        raise AppException("Email already exists", status_code=409)

    normalized_role = (role or "").strip().lower()
    if normalized_role == "resident":
        normalized_role = "homeowner"

    try:
        user_role = UserRole(normalized_role)
    except ValueError as exc:
        raise AppException("Invalid role", status_code=400) from exc

    referrer = _resolve_referrer(db, referral_code)

    resolved_name = (display_name or token_name or token_email.split("@")[0]).strip()
    user = User(
        full_name=resolved_name,
        email=token_email,
        password_hash=hash_password(str(uuid.uuid4())),
        role=user_role,
        email_verified=True,
        is_active=True,
        referred_by_user_id=referrer.id if referrer else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _issue_auth_tokens(db=db, user=user, user_agent=user_agent, ip_address=ip_address)


def rotate_refresh_token(db: Session, refresh_token: str):
    token_value = (refresh_token or "").strip()
    if not token_value:
        raise AppException("Refresh token is required", status_code=400)

    try:
        claims = decode_token(token_value)
    except ValueError as exc:
        raise AppException("Invalid or expired refresh token", status_code=401) from exc

    token_type = str(claims.get("type") or "").strip().lower()
    token_subject = str(claims.get("sub") or "").strip()
    if token_type != "refresh" or not token_subject:
        raise AppException("Invalid refresh token", status_code=401)

    session = (
        db.query(DeviceSession)
        .filter(DeviceSession.refresh_token == token_value, DeviceSession.revoked_at.is_(None))
        .first()
    )
    if not session:
        raise AppException("Invalid refresh token", status_code=401)
    if str(session.user_id) != token_subject:
        session.revoked_at = datetime.utcnow()
        db.commit()
        raise AppException("Invalid refresh token", status_code=401)
    if not session.user or not session.user.is_active:
        session.revoked_at = datetime.utcnow()
        db.commit()
        raise AppException("User not found", status_code=401)

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


def request_password_reset(db: Session, email: str, user_agent: str = "", ip_address: str = ""):
    # Prevent account enumeration: always return ok.
    login_key = (email or "").strip().lower()
    _enforce_token_issue_rate_limit(login_key=login_key, ip_address=ip_address, purpose="password_reset")
    user = db.query(User).filter(User.email == login_key).first()
    if not user or not user.is_active:
        return {"status": "ok"}

    token = generate_user_token()
    token_hash = hash_user_token(token)
    expires_at = datetime.utcnow() + timedelta(minutes=30)
    db.add(
        UserToken(
            user_id=user.id,
            token_type=UserTokenType.password_reset,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    db.commit()

    reset_link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/reset-password?email={user.email}&token={token}"
    body = (
        "You requested a password reset for your QRing account.\n\n"
        f"Reset link (expires in 30 minutes):\n{reset_link}\n\n"
        "If you did not request this, you can ignore this email."
    )
    send_email_smtp(to_email=user.email, subject="QRing password reset", body=body)

    # Token is never returned in normal responses.
    return {"status": "ok", **({"debugToken": token} if settings.DEBUG else {})}


def reset_password(db: Session, email: str, token: str, new_password: str):
    login_key = (email or "").strip().lower()
    user = db.query(User).filter(User.email == login_key).first()
    if not user or not user.is_active:
        # Avoid enumeration.
        return {"status": "ok"}

    _validate_password_strength(new_password)

    token_value = (token or "").strip()
    if not token_value:
        raise AppException("Reset token is required", status_code=400)

    token_hash = hash_user_token(token_value)
    now = datetime.utcnow()
    row = (
        db.query(UserToken)
        .filter(
            UserToken.user_id == user.id,
            UserToken.token_type == UserTokenType.password_reset,
            UserToken.token_hash == token_hash,
            UserToken.used_at.is_(None),
            UserToken.expires_at > now,
        )
        .order_by(UserToken.created_at.desc())
        .first()
    )
    if not row:
        raise AppException("Invalid or expired reset token", status_code=401)

    row.used_at = now
    user.password_hash = hash_password(new_password)
    # Revoke all sessions after password reset.
    db.query(DeviceSession).filter(DeviceSession.user_id == user.id, DeviceSession.revoked_at.is_(None)).update(
        {DeviceSession.revoked_at: now},
        synchronize_session=False,
    )
    db.commit()
    return {"status": "password_reset"}


def change_password(db: Session, user_id: str, current_password: str, new_password: str):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise AppException("User not found", status_code=404)
    if not verify_password(current_password, user.password_hash):
        raise AppException("Current password is incorrect", status_code=400)
    _validate_password_strength(new_password)
    user.password_hash = hash_password(new_password)
    now = datetime.utcnow()
    # Revoke all existing refresh sessions so stolen refresh tokens can't be used.
    db.query(DeviceSession).filter(DeviceSession.user_id == user.id, DeviceSession.revoked_at.is_(None)).update(
        {DeviceSession.revoked_at: now},
        synchronize_session=False,
    )
    db.commit()
    return {"status": "password_changed"}


def request_email_verification(db: Session, email: str, user_agent: str = "", ip_address: str = ""):
    login_key = (email or "").strip().lower()
    _enforce_token_issue_rate_limit(login_key=login_key, ip_address=ip_address, purpose="email_verify")
    user = db.query(User).filter(User.email == login_key).first()
    if not user or not user.is_active:
        # Avoid user enumeration and avoid revealing email delivery configuration.
        return {"status": "ok", "emailStatus": "ok"}
    if user.email_verified:
        return {"status": "ok", "emailStatus": "ok"}

    token = generate_user_token()
    token_hash = hash_user_token(token)
    otp = f"{secrets.randbelow(1_000_000):06d}"
    otp_hash = hash_user_token(otp)
    expires_at = datetime.utcnow() + timedelta(hours=24)
    db.add(
        UserToken(
            user_id=user.id,
            token_type=UserTokenType.email_verify,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    db.add(
        UserToken(
            user_id=user.id,
            token_type=UserTokenType.email_verify,
            token_hash=otp_hash,
            expires_at=expires_at,
            metadata_json=json.dumps({"kind": "otp", "len": 6}),
        )
    )
    db.commit()

    verify_link = (
        f"{settings.FRONTEND_BASE_URL.rstrip('/')}/verify-email"
        f"?email={quote(user.email)}&token={quote(token)}"
    )
    body = (
        "Welcome to QRing.\n\n"
        f"Verify your email (expires in 24 hours):\n{verify_link}\n\n"
        f"Or enter this OTP code in the app:\n{otp}\n\n"
        "If you did not create an account, you can ignore this email."
    )
    delivery = send_email_smtp(to_email=user.email, subject="Verify your QRing email", body=body) or {}
    email_status = str(delivery.get("status") or "unknown")
    email_reason = delivery.get("reason")
    email_message_id = delivery.get("messageId")
    payload = {
        "status": "ok",
        "emailStatus": email_status,
        "emailReason": email_reason,
        "emailMessageId": email_message_id,
    }
    if settings.DEBUG:
        payload["debugToken"] = token
        payload["debugLink"] = verify_link
    return payload


def verify_email(db: Session, email: str, token: str):
    login_key = (email or "").strip().lower()
    user = db.query(User).filter(User.email == login_key).first()
    if not user or not user.is_active:
        # Avoid enumeration.
        return {"status": "ok"}
    if user.email_verified:
        return {"status": "verified"}

    token_value = (token or "").strip()
    if not token_value:
        raise AppException("Verification token is required", status_code=400)

    _enforce_email_verify_rate_limit(login_key=login_key, ip_address="")

    token_hash = hash_user_token(token_value)
    now = datetime.utcnow()
    row = (
        db.query(UserToken)
        .filter(
            UserToken.user_id == user.id,
            UserToken.token_type == UserTokenType.email_verify,
            UserToken.token_hash == token_hash,
            UserToken.used_at.is_(None),
            UserToken.expires_at > now,
        )
        .order_by(UserToken.created_at.desc())
        .first()
    )
    if not row:
        raise AppException("Invalid or expired verification token", status_code=401)

    row.used_at = now
    user.email_verified = True
    db.commit()
    return {"status": "verified"}


def _enforce_email_verify_rate_limit(login_key: str, ip_address: str) -> None:
    # Best-effort brute-force mitigation for OTP verification attempts.
    now = datetime.utcnow().timestamp()
    key = f"verify:{(login_key or '').strip().lower()}:{(ip_address or '').strip() or 'unknown'}"
    window_seconds = 15 * 60
    max_hits = 12
    with _auth_lock:
        hits = _email_verify_attempt_hits.setdefault(key, [])
        threshold = now - window_seconds
        hits[:] = [ts for ts in hits if ts > threshold]
        hits.append(now)
        if len(hits) > max_hits:
            raise AppException("Too many verification attempts. Please try again later.", status_code=429)


def _validate_password_strength(password: str) -> None:
    value = str(password or "")
    if len(value) < _PASSWORD_MIN_LEN:
        raise AppException(f"Password must be at least {_PASSWORD_MIN_LEN} characters", status_code=400)
    if not re.search(r"[a-z]", value):
        raise AppException("Password must include a lowercase letter", status_code=400)
    if not re.search(r"[A-Z]", value):
        raise AppException("Password must include an uppercase letter", status_code=400)
    if not re.search(r"[0-9]", value):
        raise AppException("Password must include a number", status_code=400)


def _login_rate_key(login_key: str, ip_address: str, scope: str) -> str:
    ip = (ip_address or "").strip() or "unknown"
    if scope == "ip":
        return f"ip:{ip}"
    return f"ip:{ip}"


def _enforce_login_rate_limit(login_key: str, ip_address: str) -> None:
    now = datetime.utcnow().timestamp()
    keys = [
        _login_rate_key(login_key, ip_address, "ip"),
    ]
    with _auth_lock:
        for key in keys:
            blocked_until = _failed_login_blocked_until.get(key, 0.0)
            if blocked_until and now < blocked_until:
                raise AppException("Too many login attempts. Please retry later.", status_code=429)


def _record_login_failure(login_key: str, ip_address: str) -> None:
    now = datetime.utcnow().timestamp()
    keys = [
        _login_rate_key(login_key, ip_address, "ip"),
    ]
    with _auth_lock:
        for key in keys:
            hits = _failed_login_hits.setdefault(key, [])
            threshold = now - _LOGIN_FAILURE_WINDOW_SECONDS
            hits[:] = [ts for ts in hits if ts > threshold]
            hits.append(now)
            if len(hits) >= _LOGIN_MAX_FAILURES:
                _failed_login_blocked_until[key] = now + _LOGIN_LOCK_SECONDS


def _clear_login_failures(login_key: str, ip_address: str) -> None:
    keys = [
        _login_rate_key(login_key, ip_address, "ip"),
    ]
    with _auth_lock:
        for key in keys:
            _failed_login_hits.pop(key, None)
            _failed_login_blocked_until.pop(key, None)


def _token_issue_rate_key(login_key: str, ip_address: str, purpose: str) -> str:
    ip = (ip_address or "").strip() or "unknown"
    email = (login_key or "").strip().lower()
    return f"{purpose}:{ip}:{email}"


def _enforce_token_issue_rate_limit(login_key: str, ip_address: str, purpose: str) -> None:
    now = datetime.utcnow().timestamp()
    key = _token_issue_rate_key(login_key, ip_address, purpose)
    with _auth_lock:
        hits = _token_issue_hits.setdefault(key, [])
        threshold = now - _TOKEN_ISSUE_WINDOW_SECONDS
        hits[:] = [ts for ts in hits if ts > threshold]
        if len(hits) >= _TOKEN_ISSUE_MAX:
            raise AppException("Too many requests. Please retry later.", status_code=429)
        hits.append(now)
