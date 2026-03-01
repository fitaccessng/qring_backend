import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.exceptions import AppException

settings = get_settings()


def _resolve_signing_key_bytes() -> bytes:
    secret = (settings.QR_TOKEN_SIGNING_KEY or "").strip() or settings.JWT_SECRET_KEY
    return secret.encode("utf-8")


def _resolve_fernet_key() -> bytes:
    raw = (settings.QR_TOKEN_ENCRYPTION_KEY or "").strip()
    if raw:
        try:
            # Allow passing a valid base64 Fernet key directly.
            if len(raw) == 44:
                return raw.encode("utf-8")
            return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
        except Exception as exc:
            raise AppException(f"Invalid QR token encryption key: {exc}", status_code=500) from exc
    return base64.urlsafe_b64encode(hashlib.sha256(_resolve_signing_key_bytes()).digest())


def _fernet() -> Fernet:
    return Fernet(_resolve_fernet_key())


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _compute_signature(prefix: str, cipher_text: str) -> str:
    payload = f"{prefix}.{cipher_text}".encode("utf-8")
    return hmac.new(_resolve_signing_key_bytes(), payload, hashlib.sha256).hexdigest()


def build_secure_token(prefix: str, payload: dict, expires_at: datetime) -> dict:
    body = dict(payload or {})
    body["exp"] = expires_at.isoformat()
    cipher_text = _fernet().encrypt(json.dumps(body, separators=(",", ":")).encode("utf-8")).decode("utf-8")
    signature = _compute_signature(prefix, cipher_text)
    token = f"{prefix}.{cipher_text}.{signature}"
    return {
        "token": token,
        "cipherText": cipher_text,
        "signature": signature,
        "expiresAt": expires_at.isoformat(),
        "tokenHash": hash_token(token),
    }


def decode_secure_token(token: str, expected_prefix: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise AppException("Invalid token format.", status_code=400)
    prefix, cipher_text, signature = parts
    if prefix != expected_prefix:
        raise AppException("Invalid token prefix.", status_code=400)
    expected_sig = _compute_signature(prefix, cipher_text)
    if not hmac.compare_digest(signature, expected_sig):
        raise AppException("Token signature verification failed.", status_code=400)
    try:
        raw = _fernet().decrypt(cipher_text.encode("utf-8")).decode("utf-8")
        payload = json.loads(raw)
    except (InvalidToken, json.JSONDecodeError):
        raise AppException("Token decryption failed.", status_code=400)
    exp_raw = payload.get("exp")
    if not exp_raw:
        raise AppException("Token expiry metadata missing.", status_code=400)
    try:
        expires_at = datetime.fromisoformat(str(exp_raw))
    except ValueError:
        raise AppException("Token expiry metadata is invalid.", status_code=400)
    if expires_at <= datetime.utcnow():
        raise AppException("Token has expired.", status_code=400)
    return payload


def build_share_token_payload(*, appointment_id: str, homeowner_id: str) -> dict:
    return {
        "appointmentId": appointment_id,
        "homeownerId": homeowner_id,
        "scope": "appointment-share",
    }


def build_qr_token_payload(
    *,
    visitor_id: str,
    homeowner_id: str,
    appointment_id: str,
    device_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> dict:
    return {
        "visitorId": visitor_id,
        "homeownerId": homeowner_id,
        "appointmentId": appointment_id,
        "deviceId": device_id,
        "timeWindow": {
            "start": starts_at.isoformat(),
            "end": ends_at.isoformat(),
        },
    }


def token_expiry_from_now(minutes: int) -> datetime:
    return datetime.utcnow() + timedelta(minutes=max(1, int(minutes or 1)))
